"""RebacManager + RebacQuerySet.

Per ARCHITECTURE.md § Three actor-resolution paths:
  1. Per-queryset actor (.with_actor) — strictly highest priority
  2. Per-queryset sudo (.sudo) — bypass; logs an audit event
  3. current_actor() ContextVar — populated by middleware / Celery hooks
  4. Fallback — STRICT_MODE=True raises; else system_context()

The actor lives on the queryset instance (NOT a ContextVar). It survives
chaining via `_clone()` and propagates into instances via `from_db()`.
"""
from __future__ import annotations

from typing import Any, Iterable

from django.db import models

from ._id import resource_id_attr
from .actors import current_actor as _current_actor
from .actors import grant_subject_ref, to_subject_ref
from .actors import is_sudo as _is_sudo_ambient
from .conf import app_settings
from .errors import MissingActorError, NoActorResolvedError, PermissionDenied
from .types import ObjectRef, SubjectRef


class RebacQuerySet(models.QuerySet):
    """REBAC-aware queryset.

    Use `.with_actor(actor)`, `.as_user(user)`, `.as_agent(agent, on_behalf_of=u)`,
    or `.sudo(reason=...)` to scope. Materialising without any of those AND
    without an ambient actor raises `MissingActorError` when STRICT_MODE is on.
    """

    # Per-queryset state. Carried through `_clone()`.
    _rebac_actor: SubjectRef | None
    _rebac_sudo_reason: str | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rebac_actor = None
        self._rebac_sudo_reason = None

    # Note: a second `_clone` override below combines the actor + scope-flag
    # propagation; this stub kept for readability.

    # ----- Actor verbs -----

    def with_actor(self, actor: Any) -> "RebacQuerySet":
        """Pin a SubjectRef on the queryset. Generic verb — accepts any ActorLike."""
        ref = actor if isinstance(actor, SubjectRef) else to_subject_ref(actor)
        clone = self._clone()
        clone._rebac_actor = ref
        clone._rebac_sudo_reason = None
        return clone

    def as_user(self, user: Any) -> "RebacQuerySet":
        """Typed shorthand: scope to a Django User."""
        return self.with_actor(to_subject_ref(user))

    def as_agent(self, agent: Any, *, on_behalf_of: Any | None = None) -> "RebacQuerySet":
        """Typed shorthand: scope to an agent acting via a Grant."""
        return self.with_actor(grant_subject_ref(agent, on_behalf_of))

    def sudo(self, *, reason: str) -> "RebacQuerySet":
        """Bypass REBAC for this queryset. Mandatory `reason`."""
        from .actors import _sudo_state  # noqa: F401  — sanity import
        from .errors import (
            SudoNotAllowedError,
            SudoReasonRequiredError,
        )

        if not app_settings.REBAC_ALLOW_SUDO:
            raise SudoNotAllowedError("sudo() denied: REBAC_ALLOW_SUDO is False")
        if app_settings.REBAC_REQUIRE_SUDO_REASON and not reason:
            raise SudoReasonRequiredError(
                "sudo() requires reason= when REBAC_REQUIRE_SUDO_REASON=True"
            )
        clone = self._clone()
        clone._rebac_actor = None
        clone._rebac_sudo_reason = reason
        return clone

    def system_context(self, *, reason: str) -> "RebacQuerySet":
        return self.sudo(reason=reason)

    def actor(self) -> SubjectRef | None:
        return self._rebac_actor

    def is_sudo(self) -> bool:
        return self._rebac_sudo_reason is not None

    # ----- Materialisation -----

    def _resolve_effective_actor(self) -> tuple[SubjectRef | None, bool]:
        """Returns (actor_ref, is_sudo).

        Resolution order: per-queryset → ambient ContextVar → strict-mode fallback.
        """
        if self._rebac_sudo_reason is not None:
            return (None, True)
        if self._rebac_actor is not None:
            return (self._rebac_actor, False)
        if _is_sudo_ambient():
            return (None, True)
        ambient = _current_actor()
        if ambient is not None:
            return (ambient, False)
        if app_settings.REBAC_STRICT_MODE:
            raise MissingActorError(
                f"Queryset on {self.model.__name__} materialised without an actor. "
                f"Use .with_actor(actor), .as_user(user), .as_agent(agent, on_behalf_of=user), "
                f"or .sudo(reason='...'). Or set REBAC_STRICT_MODE=False (NOT recommended)."
            )
        # STRICT_MODE=False: fall through unscoped.
        return (None, True)

    _rebac_scope_applied: bool = False

    def _apply_scope_in_place(self) -> None:
        """Inject ``<id_attr>__in=<accessible>`` onto the query's WHERE clause.

        ``id_attr`` defaults to ``"pk"`` but can be flipped per-model via
        ``Meta.rebac_id_attr`` or globally via ``REBAC_RESOURCE_ID_ATTR``.

        Avoids ``self.filter(...)`` because ``.get()`` pre-slices the
        queryset and ``.filter()`` rejects post-slice. ``Q.add_q``
        operates at the SQL level and bypasses the slice check.
        """
        if self._rebac_scope_applied:
            return
        actor, sudo = self._resolve_effective_actor()
        self._rebac_scope_applied = True
        if sudo:
            return
        rebac_type = getattr(self.model._meta, "rebac_resource_type", None)
        if not rebac_type:
            return
        from django.db.models import Q

        from .backends import backend
        action = getattr(self.model._meta, "rebac_default_action", "read")
        ids = list(
            backend().accessible(
                subject=actor,  # type: ignore[arg-type]
                action=action,
                resource_type=rebac_type,
            )
        )
        attr = resource_id_attr(self.model)
        if attr == "pk":
            # Coerce to ints when the PK is integer-typed; leave as
            # strings for UUID/Char PKs. Only relevant for the pk path
            # — non-pk attrs (sqid, public_id, slug) are always
            # string-valued and pass through unchanged.
            try:
                pk_field = self.model._meta.pk
                if pk_field is not None and pk_field.get_internal_type() in (
                    "AutoField",
                    "BigAutoField",
                    "IntegerField",
                    "BigIntegerField",
                    "SmallIntegerField",
                    "PositiveIntegerField",
                    "PositiveBigIntegerField",
                    "PositiveSmallIntegerField",
                ):
                    ids = [int(i) for i in ids]
            except (ValueError, TypeError):
                pass
        # ``Q.add_q`` works even on sliced queries.
        self.query.add_q(Q(**{f"{attr}__in": ids}))

    def _clone(self, **kwargs: Any) -> "RebacQuerySet":  # type: ignore[override]
        clone = super()._clone(**kwargs)
        clone._rebac_actor = self._rebac_actor
        clone._rebac_sudo_reason = self._rebac_sudo_reason
        # Important: each clone re-applies scope when needed.
        clone._rebac_scope_applied = False
        return clone

    def _fetch_all(self) -> None:
        if self._result_cache is None:
            self._apply_scope_in_place()
        super()._fetch_all()
        if self._result_cache is not None:
            actor, _ = self._resolve_effective_actor()
            if actor is not None:
                for inst in self._result_cache:
                    if isinstance(inst, models.Model):
                        inst._rebac_actor = actor  # type: ignore[attr-defined]

    # ----- Counts / existence respect scope too -----

    def count(self) -> int:
        if self._result_cache is not None:
            return len(self._result_cache)
        self._apply_scope_in_place()
        return super().count()

    def exists(self) -> bool:
        if self._result_cache is not None:
            return bool(self._result_cache)
        self._apply_scope_in_place()
        return super().exists()

    # ----- Write ops: enforce all-or-nothing -----

    def update(self, **kwargs: Any) -> int:
        actor, sudo = self._resolve_effective_actor()
        if sudo:
            return super().update(**kwargs)
        rebac_type = getattr(self.model._meta, "rebac_resource_type", None)
        if rebac_type:
            self._guard_bulk_action(actor, "write")  # type: ignore[arg-type]
        return super().update(**kwargs)

    def delete(self) -> tuple[int, dict[str, int]]:
        actor, sudo = self._resolve_effective_actor()
        if sudo:
            return super().delete()
        rebac_type = getattr(self.model._meta, "rebac_resource_type", None)
        if rebac_type:
            self._guard_bulk_action(actor, "delete")  # type: ignore[arg-type]
        return super().delete()

    def _guard_bulk_action(self, actor: SubjectRef, action: str) -> None:
        from .backends import backend

        rebac_type = self.model._meta.rebac_resource_type  # type: ignore[attr-defined]
        attr = resource_id_attr(self.model)
        # Pre-fetch ids in scope, intersect with allowed.
        affected = {
            str(v) for v in self.values_list(attr, flat=True)
        }
        if not affected:
            return
        allowed = set(
            backend().accessible(subject=actor, action=action, resource_type=rebac_type)
        )
        denied = affected - allowed
        if denied:
            sample = ", ".join(sorted(denied)[:5])
            raise PermissionDenied(
                f"Bulk {action}: {len(denied)} row(s) outside actor scope (e.g. {sample}). "
                f"Bulk operations are all-or-nothing."
            )


class RebacManager(models.Manager.from_queryset(RebacQuerySet)):  # type: ignore[misc]
    """Manager backed by `RebacQuerySet`."""

    def get_queryset(self) -> RebacQuerySet:  # type: ignore[override]
        return RebacQuerySet(model=self.model, using=self._db, hints=self._hints)

    def with_actor(self, actor: Any) -> RebacQuerySet:
        return self.get_queryset().with_actor(actor)

    def as_user(self, user: Any) -> RebacQuerySet:
        return self.get_queryset().as_user(user)

    def as_agent(self, agent: Any, *, on_behalf_of: Any | None = None) -> RebacQuerySet:
        return self.get_queryset().as_agent(agent, on_behalf_of=on_behalf_of)

    def sudo(self, *, reason: str) -> RebacQuerySet:
        return self.get_queryset().sudo(reason=reason)

    def system_context(self, *, reason: str) -> RebacQuerySet:
        return self.sudo(reason=reason)
