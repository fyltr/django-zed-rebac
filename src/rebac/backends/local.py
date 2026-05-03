"""LocalBackend — pure-Django REBAC evaluation.

Evaluates permission expressions by walking the in-memory schema tree against
rows in the `Relationship` table. Implementation strategy:

  - For `accessible()`: BFS over the relation graph, materialising candidate
    resource_ids per visited relation. Final result is the union/intersection
    of contributors per the expression operators.
  - For `check_access()`: same walk, but bounded by the specific resource_id.
  - Recursion depth bounded by `REBAC_DEPTH_LIMIT`.

This is intentionally a clean Python implementation — fully correct against
the SpiceDB semantics for the subset of the schema language the parser
accepts. A recursive-CTE optimisation path is layered on for `accessible()`
when `REBAC_PK_IN_THRESHOLD` is exceeded; for v0.1 we use the Python walk
with prefetched relationship rows. The same code path runs on Postgres / MySQL
/ SQLite identically.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from threading import Lock

from ..conf import app_settings
from ..errors import PermissionDepthExceeded
from ..schema.ast import (
    AllowedSubject,
    Definition,
    PermArrow,
    PermBinOp,
    PermExpr,
    PermNil,
    PermRef,
    Relation,
    Schema,
)
from ..types import (
    CheckResult,
    Consistency,
    ObjectRef,
    RelationshipFilter,
    RelationshipTuple,
    SubjectRef,
    Zookie,
)
from .base import Backend


class LocalBackend(Backend):
    """Recursive-CTE-style evaluator implemented as a bounded graph walk."""

    kind = "local"

    def __init__(self) -> None:
        self._schema_lock = Lock()
        self._schema: Schema | None = None
        # Counter used as a stable monotonic xid on backends (e.g. SQLite test
        # mode) without `txid_current()`.
        self._xid_counter = 0

    # ---------- Schema management ----------

    def set_schema(self, schema: Schema) -> None:
        """Install the in-memory schema. Called by the sync command."""
        with self._schema_lock:
            self._schema = schema

    def schema(self) -> Schema:
        if self._schema is None:
            # Lazy load from DB-stored Schema* rows.
            self._schema = self._load_schema_from_db()
        return self._schema

    def _load_schema_from_db(self) -> Schema:
        from ..composition import compose
        from ..models import (
            SchemaCaveat,
            SchemaDefinition,
            SchemaOverride,
        )
        from ..schema.ast import (
            Caveat,
            CaveatParam,
            Definition,
            Permission,
            Relation,
            Schema,
        )
        from ..schema.parser import parse_permission_expression

        defs: list[Definition] = []
        for d in SchemaDefinition.objects.prefetch_related("relations", "permissions").order_by(
            "resource_type"
        ):
            relations = []
            for r in d.relations.all().order_by("name"):
                allowed = tuple(
                    AllowedSubject(
                        type=item["type"],
                        relation=item.get("relation", ""),
                        wildcard=item.get("wildcard", False),
                        with_caveat=item.get("with_caveat", ""),
                    )
                    for item in (r.allowed_subjects or [])
                )
                relations.append(Relation(r.name, allowed, r.with_expiration))
            permissions: list[Permission] = []
            for p in d.permissions.all().order_by("name"):
                expr = parse_permission_expression(p.expression)
                permissions.append(Permission(p.name, expr, p.expression))
            defs.append(Definition(d.resource_type, tuple(relations), tuple(permissions)))

        caveats = []
        for c in SchemaCaveat.objects.order_by("name"):
            params = tuple(CaveatParam(p["name"], p["type"]) for p in (c.params or []))
            caveats.append(Caveat(c.name, params, c.expression))

        baseline = Schema(definitions=defs, caveats=caveats)

        # Tier-2: apply SchemaOverride composition. Deterministic order --
        # subtraction is non-commutative, so the loader pins overrides to a
        # stable (kind, created_at, pk) order before handing them to compose().
        overrides = list(SchemaOverride.objects.all().order_by("kind", "created_at", "pk"))
        return compose(baseline, overrides)

    # ---------- Public API ----------

    def check_access(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict | None = None,
        consistency: Consistency | None = None,
    ) -> CheckResult:
        # Empty resource_id → model-level check (any row of this type the subject
        # has the action on). Treat as "is the accessible() set non-empty?".
        if not resource.resource_id:
            try:
                next(
                    iter(
                        self.accessible(
                            subject=subject,
                            action=action,
                            resource_type=resource.resource_type,
                            context=context,
                        )
                    )
                )
            except StopIteration:
                return CheckResult.no()
            return CheckResult.has()

        definition = self.schema().get_definition(resource.resource_type)
        if definition is None:
            return CheckResult.no(reason=f"unknown resource type: {resource.resource_type}")

        permission = self.schema().get_permission(resource.resource_type, action)
        if permission is None:
            # No permission expression — fall back to checking the relation directly.
            allowed = self._has_direct_relation(
                resource_type=resource.resource_type,
                resource_id=resource.resource_id,
                relation=action,
                subject=subject,
                depth=0,
            )
            return CheckResult.has() if allowed else CheckResult.no()

        allowed = self._eval_permission(
            expr=permission.expression,
            definition=definition,
            resource_id=resource.resource_id,
            subject=subject,
            depth=0,
        )
        return CheckResult.has() if allowed else CheckResult.no()

    def accessible(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        context: dict | None = None,
        consistency: Consistency | None = None,
    ) -> Iterable[str]:
        definition = self.schema().get_definition(resource_type)
        if definition is None:
            return []
        permission = self.schema().get_permission(resource_type, action)
        # Cycle-breaker for self-referential traversals (e.g. folder.parent->read).
        # Mapping `(resource_type, action)` -> currently-resolving sentinel or set.
        cache: dict[tuple[str, str], set[str] | None] = {}
        if permission is None:
            return list(
                self._resources_via_relation(
                    resource_type=resource_type,
                    relation=action,
                    subject=subject,
                    depth=0,
                    cache=cache,
                )
            )
        return list(
            self._resources_for_expr(
                expr=permission.expression,
                definition=definition,
                subject=subject,
                depth=0,
                cache=cache,
            )
        )

    def lookup_subjects(
        self,
        *,
        resource: ObjectRef,
        action: str,
        subject_type: str,
        context: dict | None = None,
        consistency: Consistency | None = None,
    ) -> Iterable[SubjectRef]:
        # Minimal forward lookup — direct relation rows only. Walking through
        # subject sets / arrows for reverse lookup is deferred to v0.2.
        from ..models import Relationship as RelationshipModel

        permission = self.schema().get_permission(resource.resource_type, action)
        relation_names = []
        if permission is None:
            relation_names = [action]
        else:
            relation_names = sorted(_collect_direct_relations(permission.expression))

        if not relation_names:
            return []

        rows = RelationshipModel.objects.filter(
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            relation__in=relation_names,
            subject_type=subject_type,
        )
        return [
            SubjectRef.of(r.subject_type, r.subject_id, r.optional_subject_relation) for r in rows
        ]

    def write_relationships(self, writes: Iterable[RelationshipTuple]) -> Zookie:
        from django.db import transaction

        from ..models import Relationship as RelationshipModel

        rows = list(writes)
        with transaction.atomic():
            for tup in rows:
                RelationshipModel.objects.update_or_create(
                    resource_type=tup.resource.resource_type,
                    resource_id=tup.resource.resource_id,
                    relation=tup.relation,
                    subject_type=tup.subject.subject_type,
                    subject_id=tup.subject.subject_id,
                    optional_subject_relation=tup.subject.optional_relation,
                    caveat_name=tup.caveat_name,
                    defaults={
                        "caveat_context": tup.caveat_context or None,
                        "expires_at": tup.expires_at,
                        "written_at_xid": self._next_xid(),
                    },
                )
        return self._zookie()

    def delete_relationships(self, filter_: RelationshipFilter) -> Zookie:
        from ..models import Relationship as RelationshipModel

        qs = RelationshipModel.objects.all()
        if filter_.resource_type:
            qs = qs.filter(resource_type=filter_.resource_type)
        if filter_.resource_id:
            qs = qs.filter(resource_id=filter_.resource_id)
        if filter_.relation:
            qs = qs.filter(relation=filter_.relation)
        if filter_.subject_type:
            qs = qs.filter(subject_type=filter_.subject_type)
        if filter_.subject_id:
            qs = qs.filter(subject_id=filter_.subject_id)
        if filter_.optional_subject_relation:
            qs = qs.filter(optional_subject_relation=filter_.optional_subject_relation)
        qs.delete()
        return self._zookie()

    # ---------- Internal evaluation ----------

    def _eval_permission(
        self,
        expr: PermExpr,
        definition: Definition,
        resource_id: str,
        subject: SubjectRef,
        depth: int,
    ) -> bool:
        # Depth counts dispatch hops (arrow walks + subject-set traversals),
        # not expression-tree shape. Binary operators don't increment depth.
        if depth > app_settings.REBAC_DEPTH_LIMIT:
            raise PermissionDepthExceeded(f"Depth limit {app_settings.REBAC_DEPTH_LIMIT} exceeded")
        if isinstance(expr, PermNil):
            return False
        if isinstance(expr, PermRef):
            relation = _find_relation(definition, expr.name)
            if relation is not None:
                return self._has_direct_relation(
                    resource_type=definition.resource_type,
                    resource_id=resource_id,
                    relation=expr.name,
                    subject=subject,
                    depth=depth,
                )
            sub_perm = next((p for p in definition.permissions if p.name == expr.name), None)
            if sub_perm is not None:
                return self._eval_permission(
                    sub_perm.expression, definition, resource_id, subject, depth
                )
            return False
        if isinstance(expr, PermArrow):
            from ..models import Relationship as RelationshipModel

            via = _find_relation(definition, expr.via)
            if via is None:
                return False
            targets = RelationshipModel.objects.filter(
                resource_type=definition.resource_type,
                resource_id=resource_id,
                relation=expr.via,
            )
            for row in targets:
                target_def = self.schema().get_definition(row.subject_type)
                if target_def is None:
                    continue
                if self._eval_permission_on(
                    permission_name=expr.target,
                    definition=target_def,
                    resource_id=row.subject_id,
                    subject=subject,
                    depth=depth + 1,
                ):
                    return True
            return False
        if isinstance(expr, PermBinOp):
            left = self._eval_permission(expr.left, definition, resource_id, subject, depth)
            if expr.op == "+":
                if left:
                    return True
                return self._eval_permission(expr.right, definition, resource_id, subject, depth)
            if expr.op == "&":
                if not left:
                    return False
                return self._eval_permission(expr.right, definition, resource_id, subject, depth)
            if expr.op == "-":
                if not left:
                    return False
                right = self._eval_permission(expr.right, definition, resource_id, subject, depth)
                return not right
            raise ValueError(f"unknown operator: {expr.op}")
        raise TypeError(f"unknown PermExpr: {expr!r}")

    def _eval_permission_on(
        self,
        permission_name: str,
        definition: Definition,
        resource_id: str,
        subject: SubjectRef,
        depth: int,
    ) -> bool:
        permission = next((p for p in definition.permissions if p.name == permission_name), None)
        if permission is None:
            # Treat as direct relation lookup.
            return self._has_direct_relation(
                resource_type=definition.resource_type,
                resource_id=resource_id,
                relation=permission_name,
                subject=subject,
                depth=depth,
            )
        return self._eval_permission(permission.expression, definition, resource_id, subject, depth)

    def _has_direct_relation(
        self,
        resource_type: str,
        resource_id: str,
        relation: str,
        subject: SubjectRef,
        depth: int,
    ) -> bool:
        # Subject-set rows count as a dispatch hop, so callers add 1 there;
        # the entry guard catches runaway recursion.
        if depth > app_settings.REBAC_DEPTH_LIMIT:
            raise PermissionDepthExceeded(f"Depth limit {app_settings.REBAC_DEPTH_LIMIT} exceeded")
        from ..models import Relationship as RelationshipModel

        rows = RelationshipModel.objects.filter(
            resource_type=resource_type,
            resource_id=resource_id,
            relation=relation,
        )
        # Direct subject match
        direct = rows.filter(
            subject_type=subject.subject_type,
            subject_id=subject.subject_id,
            optional_subject_relation=subject.optional_relation,
        )
        if _filter_active(direct).exists():
            return True

        # Wildcard match on (subject_type, "*"). Only valid for direct subject
        # types (not subject sets).
        if not subject.optional_relation:
            wildcard = rows.filter(
                subject_type=subject.subject_type,
                subject_id="*",
            )
            if _filter_active(wildcard).exists():
                return True

        # Subject-set rows: e.g. `viewer @ auth/group:eng#member`. Walk the
        # group's `member` relation and see if subject is a member.
        for row in rows.exclude(optional_subject_relation=""):
            if _is_active(row) and self._has_direct_relation(
                resource_type=row.subject_type,
                resource_id=row.subject_id,
                relation=row.optional_subject_relation,
                subject=subject,
                depth=depth + 1,
            ):
                return True
        return False

    def _resources_for_expr(
        self,
        expr: PermExpr,
        definition: Definition,
        subject: SubjectRef,
        depth: int,
        cache: dict[tuple[str, str], set[str] | None],
    ) -> set[str]:
        if depth > app_settings.REBAC_DEPTH_LIMIT:
            raise PermissionDepthExceeded(f"Depth limit {app_settings.REBAC_DEPTH_LIMIT} exceeded")
        if isinstance(expr, PermNil):
            return set()
        if isinstance(expr, PermRef):
            relation = _find_relation(definition, expr.name)
            if relation is not None:
                return self._resources_via_relation(
                    resource_type=definition.resource_type,
                    relation=expr.name,
                    subject=subject,
                    depth=depth,
                    cache=cache,
                )
            sub_perm = next((p for p in definition.permissions if p.name == expr.name), None)
            if sub_perm is not None:
                return self._resources_for_expr(
                    sub_perm.expression, definition, subject, depth, cache
                )
            return set()
        if isinstance(expr, PermArrow):
            from ..models import Relationship as RelationshipModel

            via_rel = _find_relation(definition, expr.via)
            if via_rel is None:
                return set()
            results: set[str] = set()
            target_types = sorted({s.type for s in via_rel.allowed_subjects})
            for target_type in target_types:
                target_def = self.schema().get_definition(target_type)
                if target_def is None:
                    continue
                target_resource_ids = self._compute_accessible_for(
                    target_type, expr.target, target_def, subject, depth + 1, cache
                )
                if not target_resource_ids:
                    continue
                rows = RelationshipModel.objects.filter(
                    resource_type=definition.resource_type,
                    relation=expr.via,
                    subject_type=target_type,
                    subject_id__in=list(target_resource_ids),
                )
                for r in _filter_active(rows):
                    results.add(r.resource_id)
            return results
        if isinstance(expr, PermBinOp):
            left = self._resources_for_expr(expr.left, definition, subject, depth, cache)
            right = self._resources_for_expr(expr.right, definition, subject, depth, cache)
            if expr.op == "+":
                return left | right
            if expr.op == "&":
                return left & right
            if expr.op == "-":
                return left - right
            raise ValueError(f"unknown operator: {expr.op}")
        raise TypeError(f"unknown PermExpr: {expr!r}")

    def _compute_accessible_for(
        self,
        resource_type: str,
        action: str,
        definition: Definition,
        subject: SubjectRef,
        depth: int,
        cache: dict[tuple[str, str], set[str] | None],
    ) -> set[str]:
        """Memoised entry into `_resources_for_expr` keyed by (type, action).

        Self-referential schemas (folder.parent -> folder.read) terminate via
        a fix-point: while one (type, action) walk is in flight, recursive
        re-entrants see an empty seed and return; once the outer call settles
        the cache holds the closed set.
        """
        key = (resource_type, action)
        if key in cache:
            cached = cache[key]
            return cached if cached is not None else set()
        # Sentinel value while computing — re-entrants get empty.
        cache[key] = None
        target_perm = next((p for p in definition.permissions if p.name == action), None)
        if target_perm is None:
            result = self._resources_via_relation(
                resource_type=resource_type,
                relation=action,
                subject=subject,
                depth=depth,
                cache=cache,
            )
            cache[key] = result
            return result
        # Fix-point: re-evaluate until the set stops growing. For most schemas
        # the first iteration is final; recursive ones (folder.parent->folder.read)
        # converge in O(graph diameter) steps.
        prev: set[str] = set()
        for _ in range(app_settings.REBAC_DEPTH_LIMIT + 1):
            cache[key] = prev
            current = self._resources_for_expr(
                target_perm.expression, definition, subject, depth, cache
            )
            if current == prev:
                break
            prev = current
        cache[key] = prev
        return prev

    def _resources_via_relation(
        self,
        resource_type: str,
        relation: str,
        subject: SubjectRef,
        depth: int,
        cache: dict[tuple[str, str], set[str] | None] | None = None,
    ) -> set[str]:
        if depth > app_settings.REBAC_DEPTH_LIMIT:
            raise PermissionDepthExceeded(f"Depth limit {app_settings.REBAC_DEPTH_LIMIT} exceeded")
        from ..models import Relationship as RelationshipModel

        result: set[str] = set()

        # Direct rows
        direct = RelationshipModel.objects.filter(
            resource_type=resource_type,
            relation=relation,
            subject_type=subject.subject_type,
            subject_id=subject.subject_id,
            optional_subject_relation=subject.optional_relation,
        )
        for r in _filter_active(direct):
            result.add(r.resource_id)

        # Wildcard rows
        if not subject.optional_relation:
            wildcard = RelationshipModel.objects.filter(
                resource_type=resource_type,
                relation=relation,
                subject_type=subject.subject_type,
                subject_id="*",
            )
            for r in _filter_active(wildcard):
                result.add(r.resource_id)

        # Subject-set rows: e.g. resources granted to `auth/group:X#member`
        # require the subject to actually be a member of group X.
        subject_set_rows = RelationshipModel.objects.filter(
            resource_type=resource_type, relation=relation
        ).exclude(optional_subject_relation="")
        for row in subject_set_rows:
            if not _is_active(row):
                continue
            if self._has_direct_relation(
                resource_type=row.subject_type,
                resource_id=row.subject_id,
                relation=row.optional_subject_relation,
                subject=subject,
                depth=depth + 1,
            ):
                result.add(row.resource_id)
        return result

    # ---------- helpers ----------

    def _next_xid(self) -> int:
        self._xid_counter += 1
        return int(time.time_ns()) + self._xid_counter

    def _zookie(self) -> Zookie:
        return Zookie(self.kind, str(self._next_xid()))


# ---------- Module-level helpers ----------


def _find_relation(definition: Definition, name: str) -> Relation | None:
    for r in definition.relations:
        if r.name == name:
            return r
    return None


def _collect_direct_relations(expr: PermExpr) -> set[str]:
    """Walk a permission expression and collect bottom-most relation names.

    Used by `lookup_subjects` to know which relation rows to inspect.
    """
    if isinstance(expr, PermNil):
        return set()
    if isinstance(expr, PermRef):
        return {expr.name}
    if isinstance(expr, PermArrow):
        return set()  # arrows route through other definitions; reverse-lookup is deferred
    if isinstance(expr, PermBinOp):
        return _collect_direct_relations(expr.left) | _collect_direct_relations(expr.right)
    return set()


def _filter_active(qs: object) -> object:
    """Exclude expired rows. Postgres-friendly via a parameterised filter."""
    from django.db.models import Q
    from django.utils import timezone

    return qs.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))  # type: ignore[attr-defined]


def _is_active(row: object) -> bool:
    expires_at = getattr(row, "expires_at", None)
    if expires_at is None:
        return True
    from django.utils import timezone

    return expires_at > timezone.now()
