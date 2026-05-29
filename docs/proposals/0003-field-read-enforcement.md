# Proposal 0003 — Field-level read enforcement: `read__<field>` gates with a configurable deny mode (`redact` / `omit` / `raise`)

**Target version:** v0.7 (additive; default off — `REBAC_FIELD_READ_MODE = "allow"`).
**Status:** Draft — for maintainer review.
**Scope:** `LocalBackend` + `SpiceDBBackend` (backend-oblivious), `managers.py`
(queryset materialisation), `mixins.py` (instance scope + write-exclusion),
`signals.py` (write-exclusion of redacted fields), `schema/walker.py` (one
shared field-gate accessor), `conf.py` + `checks.py`. **No `.zed` grammar,
parser, AST, or `Backend` ABC change.** The descriptor-based `raise`-everywhere
tier stays the existing **1.x `Meta.protected_fields`** roadmap item
(`ARCHITECTURE.md` Roadmap); this proposal ships `redact` / `omit` and reserves
`raise`.

---

## Why

The package already enforces **per-field _write_ gates** end-to-end: a schema
that declares `permission write__title = owner` is enforced on instance saves
(`signals._enforce_per_field_writes`, `signals.py:148`) and on bulk updates
(`managers._guard_bulk_field_writes`, `managers.py:315`), against any transport.
The schema introspection is a one-liner —
`{p.name for p in definition.permissions if p.name.startswith("write__")}`
(`signals.py:181`, `managers.py:344`) — and the test suite proves it
(`tests/test_field_gates.py`).

The **read** half does not exist. The `.zed` parser already accepts
`permission read__salary = owner` (permission names are plain identifiers — no
`write__`/`read__` special-casing anywhere in `schema/parser.py`), but nothing
in the package enforces it. So today the only consumer that gates field reads is
the downstream GraphQL layer, which hand-rolls it: it re-derives the
`read__<field>` action from the schema, installs a per-field gated resolver, and
returns `None` on deny. That has three problems:

1. **Transport asymmetry.** Only GraphQL gets field-read protection. The DRF
   integration (`RebacPermission` / `RebacFilterBackend`), the admin, a second
   transport, and plain ORM code all read every column unguarded. Row scoping
   and field _writes_ are enforced in the model for *all* transports; field
   _reads_ are the lone residual that lives outside it.
2. **Policy lives in the wrong place.** "Which fields are gated" and "is this
   actor allowed" are REBAC decisions. Re-deriving them in a resolver duplicates
   what `signals.py:181` already does for writes and lets the two drift.
3. **The read→write loop is open.** A consumer that redacts a field to `None`
   for display can hand that same instance to `.save()` and silently persist
   `None` over the real value. Only the model layer — which owns the write
   path — can close that loop.

Reads are genuinely subtler than writes, which is why this lands as its own
tier rather than a mirror of the write gate:

- A write has one choke point (`pre_save` / bulk `update`) and one correct
  failure mode (`raise`). A read happens on every attribute access, lazily,
  including from internal logic (the owner check itself reads fields). A
  descriptor that checks-and-raises on `__get__` is a perf and correctness
  footgun and breaks `.only()` / `.defer()`.
- The right behaviour on a denied read is usually **redaction** (a presentation
  decision), not a hard error.

So this proposal moves the **policy and the redaction primitive** into the
package behind a setting + per-scope override, ships the two safe modes
(`redact`, `omit`) at queryset-materialisation time, and defers the
descriptor-based `raise`-everywhere mode to the `Meta.protected_fields` 1.x
item it already belongs to.

---

## Decisions already locked

1. **Schema side is zero-change.** A field-read gate is an ordinary permission
   named `read__<field>`. Discovery mirrors the write path; we factor the
   existing inline write-discovery into one shared accessor (below) and add the
   `read__` caller. No grammar, parser, AST, or `Backend` ABC change.

2. **One global mode + a per-scope override.** `REBAC_FIELD_READ_MODE` sets the
   default; `.on_field_deny(mode)` overrides it on a queryset / manager / pinned
   instance, exactly like `.with_action(...)` overrides the read action. Modes:
   `"allow"` (default; no field enforcement — current behaviour),
   `"redact"`, `"omit"`, `"raise"`.

3. **Per-row semantics, computed with the row-scoping engine — NOT a blanket
   `.defer()`.** This is the load-bearing correctness decision. A gate like
   `read__salary = owner` is *per-row*: Alice may read `salary` on her own row
   but not on Bob's. So for each declared `read__<f>` we compute the visible-id
   set with the **same verb row scoping uses** —
   `accessible(subject, action="read__<f>", resource_type)` (`managers.py:189`)
   — and redact the field on materialised rows whose id is **not** in that set.
   A whole-queryset `.defer(field)` would wrongly hide the field on rows the
   actor *can* read; it is only valid as an optimisation for the all-denied
   case (visible-id set empty) or a provably row-independent gate, never the
   default.

4. **`redact` and `omit` share one per-row engine; they differ only at the
   projection boundary.** `redact` → the attribute is set to `None` and recorded
   in `_rebac_redacted_fields`. `omit` → identical computation, but the consumer
   projection (GraphQL resolver, DRF serializer) drops the recorded field rather
   than emitting `null`. You cannot SQL-exclude a column for *some* rows, so
   per-row "omit" is realised in Python, not in the SELECT. The only SQL-level
   `.defer()` fast path is the all-denied case.

5. **Redacted fields are excluded from writes (the closed loop).** Redaction
   records `_rebac_redacted_fields` on the instance. The existing `pre_save`
   path (`signals.py`) then: raises `PermissionDenied` if a redacted field is
   explicitly named in `save(update_fields=…)` (the caller is writing a field
   they could not read — fail closed), and auto-excludes recorded redacted
   fields from a full `save()`. A redacted instance can never persist `None`
   over the real value.

6. **Three-state is honoured on the instance path; the bulk path is
   fail-closed on `CONDITIONAL`.** A `read__<f>` may carry a caveat, so
   `check_access` can return `CONDITIONAL_PERMISSION` when context is missing
   (`CheckResult`, `types.py:77`). On the instance path
   (`instance.with_actor(actor).check_access("read__f", context=…)`) the caller
   can supply context and gets the real three-state answer. At bulk
   materialisation there is no per-row caveat context, so a row that is only
   *conditionally* visible is treated as denied (redacted) — consistent with
   `has_access` collapsing `CONDITIONAL → False` (`mixins.py:360`). Tunable via
   `REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL` (default `True`).

7. **Field enforcement runs exactly when row scoping runs.** Under `sudo()` /
   `system_context()` there is no redaction (full visibility), same as row
   scope. No-actor + strict mode already raises in
   `_resolve_effective_actor` (`managers.py:125`). A model with no
   `rebac_resource_type`, or a schema with no `read__*` permissions declared, is
   a cheap no-op (the common case — one `startswith` scan, no extra query).

8. **`raise` is reserved here, delivered with `Meta.protected_fields` (1.x).**
   Transparent "raise on any unauthorised attribute access, in any transport"
   requires descriptors on the model fields — the existing 1.x
   `Meta.protected_fields` roadmap item. This proposal **accepts** the `"raise"`
   value and the `.on_field_deny("raise")` API but, until the descriptor tier
   lands, degrades it to `redact` and surfaces `rebac.W008`. `redact` / `omit`
   need no descriptors and ship now.

9. **Backend-oblivious; SpiceDB-portable.** Every check is a plain
   `accessible(action="read__<f>")` / `check_access("read__<f>")` call — no new
   backend method, no ABC change. The mode/redaction is a Python presentation
   layer atop the verdict. The per-field `accessible()` calls route through the
   request-scoped evaluator (Proposal 0002), so a model with K field gates
   materialised many times in one request collapses to K graph walks, not
   K×resolvers.

---

## Concrete API

### Settings — `src/rebac/conf.py`

```python
# Default deny behaviour for read__<field> gates.
"REBAC_FIELD_READ_MODE": "allow",          # | "redact" | "omit" | "raise"
# A conditionally-visible field (caveat context absent) is denied at bulk
# materialisation. False = treat conditional as visible (NOT recommended).
"REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL": True,
```

`"allow"` is the default so existing deployments see no behaviour change; a
consumer (or the shipped GraphQL adapter) opts in by setting the mode globally
or per scope.

### The mode type + per-scope override — `src/rebac/managers.py`

```python
FieldDenyMode = Literal["allow", "redact", "omit", "raise"]

class RebacQuerySet(models.QuerySet[_M]):
    # Joins the existing per-queryset state carried through `_clone`
    # (`managers.py:41-50, 231-240`): _rebac_actor, _rebac_action, …
    _rebac_field_deny: FieldDenyMode | None  # None → fall back to the setting

    def on_field_deny(self, mode: FieldDenyMode) -> RebacQuerySet[_M]:
        """Override REBAC_FIELD_READ_MODE for this queryset.

        Chainable, like .with_action(). Propagated to clones. The manager
        exposes the same verb via `from_queryset`, so
        `Note.objects.on_field_deny("omit")` works.
        """
        clone = self._clone()
        clone._rebac_field_deny = mode
        return clone
```

### Materialisation hook — extend the existing post-fetch loop

`_fetch_all` (`managers.py:242-251`) already iterates the result cache after the
SELECT to pin `_rebac_actor` on each instance. Field redaction hangs off the
same loop — no second pass:

```python
def _fetch_all(self) -> None:
    if self._result_cache is None:
        self._apply_scope_in_place()
    super()._fetch_all()
    if self._result_cache is not None:
        actor, sudo = self._resolve_effective_actor()
        if actor is not None and not sudo:
            for inst in self._result_cache:
                if isinstance(inst, models.Model):
                    inst._rebac_actor = actor
            # NEW — one accessible() per declared read__<f>, evaluator-cached;
            # nulls each gated field on rows outside its visible-id set and
            # records `_rebac_redacted_fields` on those instances.
            apply_field_visibility(
                self._result_cache,
                model=self.model,
                actor=actor,
                mode=self._effective_field_mode(),
            )
```

### The visibility engine — `src/rebac/field_visibility.py` (NEW)

```python
def gated_read_fields(model: type[Model]) -> frozenset[str]:
    """The set of field names with a `read__<field>` permission declared on
    this model's resource type. Empty (cheap no-op) for the common case.
    Uses the shared `field_gated_actions(definition, "read")` accessor.
    """

def visible_id_sets(
    *, model, actor, fields: frozenset[str], evaluator=None,
) -> dict[str, frozenset[str]]:
    """For each gated field, the resource-id set the actor may read it on —
    `accessible(subject=actor, action="read__<field>", resource_type=…)`,
    routed through the request evaluator when one is open (Proposal 0002).
    """

def apply_field_visibility(instances, *, model, actor, mode) -> None:
    """Apply `mode` to a fetched batch. `allow` → no-op. `redact`/`omit` →
    for each gated field, null it on instances whose id is absent from its
    visible-id set, recording `_rebac_redacted_fields`. `raise` → degrade to
    `redact` + warn once (W008) until the Meta.protected_fields descriptor
    tier lands. Sets `instance._rebac_omitted_fields` for `omit` so the
    projection boundary can drop them instead of emitting null.
    """
```

### Instance scope — `src/rebac/mixins.py`

`RebacMixin.with_actor` already pins `_rebac_actor` and returns `Self`
(`mixins.py:238`). Add the read-side counterparts that join `check_access` /
`has_access` (`mixins.py:303,360`):

```python
class RebacMixin:
    def denied_read_fields(
        self, *, context: dict[str, Any] | None = None,
    ) -> frozenset[str]:
        """The gated fields the pinned actor may NOT read on THIS row.
        Pure decision — no mutation. Honours three-state (a CONDITIONAL
        field with no context counts as denied per the fail-closed setting).
        Lets a consumer drive its own projection without redacting in place.
        """

    def with_field_deny(self, mode: FieldDenyMode) -> Self:
        """Per-instance override of REBAC_FIELD_READ_MODE (chains off with_actor)."""

    def redacted(
        self, *, mode: FieldDenyMode | None = None,
        context: dict[str, Any] | None = None,
    ) -> Self:
        """Apply the deny mode to this instance and return it: denied fields
        nulled + recorded. Eager and explicit — no descriptors. Used by the
        hand-fetched single-instance path (`obj.with_actor(u).redacted()`).
        """
```

### Shared schema accessor — `src/rebac/schema/walker.py`

Factor the inline write discovery (`signals.py:181`, `managers.py:344`) into one
home next to `find_permission` / `find_relation`, then add the `read__` caller —
DRY, no behaviour change to writes:

```python
def field_gated_actions(definition: Definition, verb: str) -> frozenset[str]:
    """Permission names of the form `<verb>__<field>` on this definition.
    `field_gated_actions(defn, "write")` reproduces the set both write-gate
    sites compute today; `field_gated_actions(defn, "read")` is the new caller.
    """
    prefix = f"{verb}__"
    return frozenset(p.name for p in definition.permissions if p.name.startswith(prefix))
```

### Write-exclusion — `src/rebac/signals.py`

In `_rebac_pre_save`, after the resource-level + `write__` checks, before the
row hits the DB:

```python
# Intent (exact mechanism is an implementation detail of the save path):
redacted = getattr(instance, "_rebac_redacted_fields", frozenset())
if redacted:
    if update_fields is not None and (bad := redacted & set(update_fields)):
        # Caller is writing a field they could not read — fail closed.
        raise PermissionDenied(
            f"Cannot write field(s) {sorted(bad)} on {resource}: "
            f"they were redacted on read (read__<field> denied)."
        )
    # Full save (update_fields is None): the redacted fields must be
    # dropped from the column set the UPDATE writes, so the placeholder
    # None never overwrites the stored value. Implemented by deriving an
    # update_fields that excludes `redacted`, or by signalling save() to
    # skip them — whichever the save path expresses cleanly.
```

---

## Settings + system checks

```python
# src/rebac/conf.py
"REBAC_FIELD_READ_MODE": "allow",
"REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL": True,
```

- **`rebac.E008`** — `REBAC_FIELD_READ_MODE` must be one of
  `("allow", "redact", "omit", "raise")`.
- **`rebac.W008`** — `REBAC_FIELD_READ_MODE = "raise"` (or any
  `.on_field_deny("raise")`) while the `Meta.protected_fields` descriptor tier
  is not yet available → `"raise"` degrades to `"redact"`; will become a hard
  mode in the 1.x descriptor release.

(Codes `E001–E007`, `E010`, `W001–W007` are taken; `E008` and `W008` are free.)

---

## LocalBackend / SpiceDBBackend

No backend change. Both already implement
`accessible(subject, action, resource_type)`; this proposal calls it with
`action="read__<field>"`, which is just another action over the same graph. The
`LocalBackend` recursive CTE and the `SpiceDBBackend` gRPC `LookupResources`
both resolve it with no new code. The evaluator's `accessible` cache (Proposal
0002) dedups the per-field walks within a request scope.

---

## Files to touch

| File | Change |
|---|---|
| `src/rebac/field_visibility.py` | NEW — `gated_read_fields`, `visible_id_sets`, `apply_field_visibility` |
| `src/rebac/schema/walker.py` | NEW `field_gated_actions(definition, verb)`; used by read path |
| `src/rebac/managers.py` | `_rebac_field_deny` state + `_clone` propagation; `on_field_deny`; `_effective_field_mode`; redaction call in `_fetch_all` post-fetch loop |
| `src/rebac/mixins.py` | `denied_read_fields`, `with_field_deny`, `redacted` instance methods |
| `src/rebac/signals.py` | Refactor inline `write__` discovery to `field_gated_actions`; add redacted-field write-exclusion in `_rebac_pre_save` |
| `src/rebac/conf.py` | `REBAC_FIELD_READ_MODE`, `REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL` |
| `src/rebac/checks.py` | `rebac.E008`, `rebac.W008` |
| `src/rebac/__init__.py` | Export `FieldDenyMode`; `redacted` / `denied_read_fields` reachable via mixin |
| `tests/test_field_read_gates.py` | NEW — the matrix below |
| `tests/test_field_gates.py` | Add a regression that `write__` discovery still matches after the `field_gated_actions` refactor |
| `README.md` | Highlights bullet: "field-level read gates (`read__<field>`) with redact/omit modes, enforced in the model for every transport" |
| `docs/ARCHITECTURE.md` | New "Field-level read enforcement" section under the permission model; cross-link the 1.x `Meta.protected_fields` row to this proposal |
| `docs/ZED.md` | Note that `read__<field>` is a first-class field gate, symmetric to `write__<field>` |
| `CHANGELOG.md` | 0.7 entry |

---

## Test coverage required

1. **No gates declared → no extra query.** A model with no `read__*` permission
   materialises with zero `accessible()` calls beyond row scoping.
2. **Per-row redaction.** `read__salary = owner`; Alice and Bob both visible in
   a list; Alice's row keeps `salary`, Bob's is `None` for the Alice actor and
   vice-versa. The single most important test — proves it is **not** a blanket
   defer.
3. **`redact` vs `omit`.** Same computation; `redact` leaves `salary = None` and
   records `_rebac_redacted_fields`; `omit` records `_rebac_omitted_fields` so a
   projection can drop the key.
4. **Owner sees everything.** Owner of all rows → no field nulled, no extra
   redaction work.
5. **Write-exclusion — full save.** Fetch a redacted instance, mutate an
   unrelated field, `save()` → the redacted field is NOT written; stored value
   intact.
6. **Write-exclusion — explicit `update_fields`.** `save(update_fields=["salary"])`
   on a redacted instance → `PermissionDenied`.
7. **Caveat / three-state on the instance path.** `read__salary = owner with
   business_hours`; `instance.with_actor(hr).denied_read_fields(context={"now": …})`
   returns `salary` outside hours, not within.
8. **Bulk fail-closed on conditional.** Same gate, bulk materialise with no
   context → conditionally-visible rows redact the field
   (`REBAC_FIELD_READ_FAIL_CLOSED_ON_CONDITIONAL=True`); flips when set `False`.
9. **`sudo` / `system_context` → no redaction.** A sudo queryset returns all
   fields populated.
10. **No-actor + strict mode.** Unchanged — `_resolve_effective_actor` still
    raises `MissingActorError` before redaction is reached.
11. **`on_field_deny` override + clone propagation.** `.on_field_deny("omit")`
    survives `.filter()` / `.order_by()` clones; overrides the global setting.
12. **`raise` degradation.** `REBAC_FIELD_READ_MODE="raise"` behaves as `redact`
    and emits `W008` once (descriptor tier not present).
13. **`field_gated_actions` refactor parity.** Every existing `write__` gate
    test in `tests/test_field_gates.py` passes unchanged.
14. **System checks.** `E008` on an invalid mode; `W008` on `"raise"`.
15. **Counts / aggregates unaffected.** `count()` / `exists()` (which call
    `_apply_scope_in_place` but not `_fetch_all`’s redaction loop) are
    row-scoped only — field redaction never changes row counts.

---

## Out of scope — do NOT change

- **`raise`-everywhere via descriptors** — that is the 1.x `Meta.protected_fields`
  item. This proposal reserves the mode and degrades it.
- **`Backend` ABC** (`check_access`, `accessible`, `write_relationships`, …) —
  unchanged; field reads are an action over the existing surface.
- **The `.zed` grammar, parser, AST.** `read__<field>` is already a legal
  permission name.
- **Row scoping semantics** (`_apply_scope_in_place`) — untouched; field
  visibility is a strictly-additional post-fetch pass.
- **Write gates** (`write__<field>`) — behaviour identical; only the discovery
  one-liner is factored into `field_gated_actions`.
- **SpiceDB schema push / sync** — no schema-shape change to push.
- **A per-field SQL `.defer()` optimisation for row-independent gates** — noted
  as a future fast path (detect a gate whose expression references no
  per-resource relation); not in this proposal. The all-denied empty-set case
  may `.defer()` trivially.

---

## Acceptance

- All existing tests pass unchanged, including the `write__` suite after the
  `field_gated_actions` refactor.
- New `tests/test_field_read_gates.py` (~15 cases above) passes on
  `LocalBackend`; the per-row redaction and write-exclusion cases are the gating
  ones.
- Default `REBAC_FIELD_READ_MODE="allow"` is a provable no-op: a deployment that
  does not set the mode and declares no `read__*` permission fires zero extra
  `accessible()` calls and redacts nothing.
- `mypy src/rebac/field_visibility.py` and the touched modules report no new
  errors.
- `README.md` + `docs/ARCHITECTURE.md` + `docs/ZED.md` + `CHANGELOG.md` updated;
  the 1.x `Meta.protected_fields` roadmap row cross-links here.

---

## Rollout plan

- **0.7** (this proposal) ships `redact` / `omit` at materialisation +
  write-exclusion + the instance API, default `"allow"`. Existing consumers
  unaffected. The shipped Strawberry adapter (Proposal 0002) and any downstream
  GraphQL layer stop hand-rolling field redaction: they set
  `REBAC_FIELD_READ_MODE="redact"` (or `.on_field_deny("omit")` on projected
  queries) and delete their per-field resolver gating — field-read policy then
  resolves identically for DRF, admin, the ORM, and any second transport.
- **0.7.x** — observe; consider the row-independent `.defer()` fast path if
  wide gates appear in the wild.
- **1.x** — `Meta.protected_fields` lands the descriptor tier; `"raise"` becomes
  a real transparent mode and `W008` is removed. `redact` / `omit` remain the
  default, descriptor-free path.

---

## Context from prior work (read before touching shared files)

- **Proposal 0002** added the request-scoped `PermissionEvaluator`
  (`src/rebac/evaluator.py`) and `current_evaluator()`. `_apply_scope_in_place`
  already routes `accessible()` through it (`managers.py:186-204`); the new
  per-field `accessible(action="read__<f>")` calls must do the same so they
  share the cache.
- **Proposal 0001** added the `RelationshipRegistry` storage mode. Field reads
  call `accessible()` like row scoping, so they are storage-mode agnostic — no
  registry-specific code.
- `managers.py:242-251` — `_fetch_all` already post-processes the result cache
  to pin `_rebac_actor`; the redaction pass extends that loop rather than adding
  a second iteration.
- `signals.py:148-201` + `managers.py:315-374` — the `write__<f>` enforcement
  this read tier is symmetric to; the `field_gated_actions` refactor touches
  both. Pull from `main` first; these are the shared files.
- `CheckResult` (`types.py:77-103`) — `allowed` / `result` / `conditional_on`;
  `has_access` collapses `CONDITIONAL → False` (`mixins.py:360`), which is the
  bulk-path fail-closed default reused here.
