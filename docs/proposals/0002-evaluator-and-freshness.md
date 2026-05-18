# Proposal 0002 — Request-scoped `PermissionEvaluator` + Zookie freshness ContextVar + Strawberry/Channels adapter

**Target version:** v0.4 (additive; default-on for HTTP, opt-in scope for WS).
**Status:** Draft — pending maintainer approval.
**Scope:** `LocalBackend` + `SpiceDBBackend`, `actors.py`, `middleware.py`,
new `rebac.graphql.strawberry` adapter behind a `[strawberry]` extra.

---

## Why

Two latent issues become acute the moment a consumer wires up GraphQL over
WebSocket subscriptions:

**(a) N+1 permission checks.** A GraphQL query like
`{ posts { title author { name } } }` returning 50 posts can fire 50+
`has_access(post, "read")` checks plus 50+ `has_access(author, "read")` —
each a recursive-CTE walk in `LocalBackend` or a gRPC round-trip in
`SpiceDBBackend`. We have a per-request `_accessible_cache` ContextVar but
no equivalent for `check_access()`. Strawberry's resolution fan-out turns
this into the dominant request cost.

**(b) Write-then-read staleness.** A mutation that runs
`write_relationships(...)` then queries `accessible()` against
`SpiceDBBackend` under default `minimize_latency` reads pre-write data
for up to ~5s due to SpiceDB's dispatcher cache. This is the entire
`Zookie` mechanism's reason for existing in SpiceDB — we expose `Zookie`
and `Consistency` as public types but never thread the post-write token
through to subsequent reads.

For WebSocket subscriptions both issues compound: emissions fire many
checks, and subscriptions are by definition write-triggered (some change
caused the emission), so every emission is a write-then-read sequence
against potentially stale dispatcher state.

`django-spicedb` (the only comparable Django package — see survey in
session log) ships a `WriteTokenMiddleware` + `PermissionEvaluator` pair
that addresses both. This proposal adapts the pattern to our architecture
without inheriting its anti-patterns (session-coupling, `threading.local`,
`RebacMeta`-as-schema).

---

## Decisions already locked

1. **One unified evaluator owns both caches.** The existing
   `_accessible_cache` ContextVar collapses into a single
   `PermissionEvaluator` instance held in a new `_current_evaluator`
   ContextVar. `accessible_cached()` keeps its public signature but
   delegates to the evaluator; a new `check_cached()` does the same for
   point checks. Single lifecycle to reason about.
2. **Per-request lifetime for HTTP, per-emission for WS.** `ActorMiddleware`
   continues to open/close the scope around an HTTP request. The
   Strawberry extension opens/closes per *operation* for HTTP GraphQL
   and per *subscription tick* for WS. Long-lived WS connections never
   hold a cache across emissions — revoked grants take effect at the
   next tick.
3. **ContextVar transport for Zookie; opt-in cross-request transport.**
   The ambient transport between calls within a single request is a
   `_last_zookie` ContextVar (always on). Cross-request transport for
   SPAs / JWT clients is configurable via `REBAC_ZOOKIE_TRANSPORT`:
   `"none"` (default), `"header"` (X-Rebac-Zookie request/response
   header), or `"session"` (django.contrib.sessions). Session is
   explicitly NOT the default because the package must not depend on
   any specific session backend.
4. **LocalBackend freshness witness.** `Zookie.kind = "local"`,
   `Zookie.token = str(<monotonic_xid>)`. `at_least_as_fresh(zookie)`
   reads become `WHERE written_at_xid <= cutoff` in the active
   `Relationship`/`RelationshipRegistry` query. Same backend-agnostic
   public API; the local witness is the existing `written_at_xid`
   column already populated on every write.
5. **Strawberry adapter behind `[strawberry]` extra.** `pip install
   django-zed-rebac[strawberry]` is the gate; the module
   `rebac.graphql.strawberry` raises `ImportError` with a remediation
   message if imported without the extra. Channels integration is
   bundled in the same module (Channels is a Strawberry transport
   prerequisite for WS subscriptions; not a separate extra).
6. **Backend protocol unchanged.** `Backend.check_access` and
   `Backend.accessible` keep their existing signatures. The evaluator
   wraps them; backends are oblivious. SpiceDB compatibility unaffected.

---

## Concrete API

### `rebac.evaluator` — new module

```python
# src/rebac/evaluator.py
class PermissionEvaluator:
    """Per-scope cache for ``check_access`` and ``accessible`` calls.

    Cache keys:
      - check: ``(str(subject), action, str(resource), frozenset(context))``
      - accessible: ``(str(subject), action, resource_type, frozenset(context))``

    Bounded by ``REBAC_EVALUATOR_CACHE_SIZE`` (default 10_000 entries
    total across both caches; LRU eviction). Adversarial queries can't
    blow out memory.

    Conditional results are NOT cached — the caveat context is part of
    the answer and the next call may supply different values. We cache
    only fully-resolved HAS_PERMISSION / NO_PERMISSION verdicts.
    """
    def __init__(self, *, max_size: int = 10_000) -> None: ...
    def check(self, *, subject, action, resource, context=None, consistency=None) -> CheckResult: ...
    def accessible(self, *, subject, action, resource_type, context=None, consistency=None) -> tuple[str, ...]: ...
    def invalidate(self) -> None:
        """Drop every cached entry. Called per-emission by the subscription bracket."""

def current_evaluator() -> PermissionEvaluator | None:
    """The ambient evaluator. None when no middleware/extension has opened a scope."""

@contextmanager
def evaluator_scope() -> Iterator[PermissionEvaluator]:
    """Open a fresh evaluator scope. Yields the new evaluator instance.

    LIFO via ContextVar; safe across await boundaries. The yielded
    evaluator is the one ``current_evaluator()`` will return until the
    block exits.
    """
```

### `rebac.consistency` — new module

```python
# src/rebac/consistency.py
def current_zookie() -> Zookie | None:
    """The most-recently-written Zookie in this scope, or None."""

def record_zookie(zookie: Zookie) -> None:
    """Stash a post-write Zookie in the ContextVar. Called by
    ``write_relationships`` and ``delete_relationships`` automatically."""

@contextmanager
def zookie_scope(initial: Zookie | None = None) -> Iterator[None]:
    """Open a fresh Zookie ContextVar slot.

    ActorMiddleware brackets one per HTTP request. The Strawberry
    extension brackets one per GraphQL operation (HTTP) or per
    subscription tick (WS). The optional ``initial`` argument carries
    a token rehydrated from a header / session / WS-attach payload.
    """

def effective_consistency(
    explicit: Consistency | None,
) -> Consistency | None:
    """If the caller supplied a consistency, pass it through. Otherwise,
    upgrade to ``at_least_as_fresh(current_zookie())`` if a Zookie is
    in scope. Otherwise return None (backend default = minimize_latency).
    """
```

### `rebac.types` extensions

```python
# already public
@dataclass(frozen=True)
class Zookie:
    kind: str    # "local" | "spicedb"
    token: str

@dataclass(frozen=True)
class Consistency:
    mode: Literal["minimize_latency", "fully_consistent", "at_least_as_fresh", "exact_snapshot"]
    zookie: Zookie | None = None   # required when mode in ("at_least_as_fresh", "exact_snapshot")

    @classmethod
    def at_least_as_fresh(cls, zookie: Zookie) -> "Consistency":
        return cls(mode="at_least_as_fresh", zookie=zookie)
```

If the current `Consistency` doesn't already have this shape, the
existing surface keeps working; we add the classmethod constructors.

### `rebac.middleware.ActorMiddleware` — updated

```python
def __call__(self, request):
    resolver = get_actor_resolver()
    actor_ref = resolver(request)
    actor_token = _current_actor.set(actor_ref)

    # Replaces the old enable_accessible_cache / disable_accessible_cache
    # pair. Single scope, one evaluator instance.
    with evaluator_scope() as evaluator:
        with zookie_scope(initial=self._rehydrate_zookie(request)):
            try:
                if use_sudo:
                    with sudo(reason="superuser-bypass"):
                        response = self.get_response(request)
                else:
                    response = self.get_response(request)
            finally:
                _current_actor.reset(actor_token)
            self._persist_zookie(response)   # header transport, opt-in
            return response
```

### `rebac.graphql.strawberry` — new module (behind `[strawberry]` extra)

```python
# src/rebac/graphql/strawberry.py

class RebacExtension(SchemaExtension):
    """Strawberry extension. Opens an evaluator + zookie scope per
    GraphQL operation. Subscription emissions get fresh scopes via
    on_operation per yield.

    Use::

        import strawberry
        from rebac.graphql.strawberry import RebacExtension

        schema = strawberry.Schema(query=Query, extensions=[RebacExtension])
    """
    def on_operation(self):
        with evaluator_scope():
            with zookie_scope():
                yield


class RebacChannelsConsumerMixin:
    """Mixin for Channels JsonWebsocketConsumer / AsyncJsonWebsocketConsumer.

    - At handshake (``connect``), resolves the actor from
      ``self.scope["user"]`` via the configured resolver and stashes it
      in the connection-level ContextVar.
    - Each subscription emission gets a fresh evaluator + zookie scope
      via the RebacExtension on the underlying Schema.

    This is the recommended path for Strawberry+Channels subscriptions.
    """
    async def connect(self):
        from rebac.actors import to_subject_ref, set_current_actor
        from rebac.errors import NoActorResolvedError
        user = self.scope.get("user")
        try:
            self._rebac_actor = to_subject_ref(user) if user else None
        except NoActorResolvedError:
            self._rebac_actor = None
        if self._rebac_actor is not None:
            set_current_actor(self._rebac_actor)
        await super().connect()
```

### Hook into `write_relationships` / `delete_relationships`

```python
# src/rebac/relationships.py — minimal addition
def write_relationships(writes):
    ...
    zookie = backend().write_relationships(rows)
    record_zookie(zookie)   # NEW — stashes in ContextVar
    ...
    return zookie
```

### `Backend.check_access` / `Backend.accessible` — internally route through evaluator

`backend()` returns the singleton; callers don't change. But the public
helpers `has_access(op)` / `check_access(op)` / `accessible(op)` defined
in `rebac/__init__.py` (or equivalent) consult `current_evaluator()`
when present and call the underlying backend method only on miss.

---

## Settings

```python
# src/rebac/conf.py
"REBAC_EVALUATOR_CACHE_SIZE": 10_000,
"REBAC_ZOOKIE_TRANSPORT": "none",           # | "header" | "session"
"REBAC_ZOOKIE_HEADER_NAME": "X-Rebac-Zookie",
"REBAC_ZOOKIE_SESSION_KEY": "_rebac_zookie",
```

System checks:

- **`rebac.E007`** — `REBAC_ZOOKIE_TRANSPORT` must be one of
  `("none", "header", "session")`.
- **`rebac.W006`** — `REBAC_ZOOKIE_TRANSPORT="session"` but
  `django.contrib.sessions` not in `INSTALLED_APPS` → warn (will fail
  at request time with KeyError otherwise).
- **`rebac.W007`** — `rebac.graphql.strawberry` imported but the
  `strawberry-graphql` package isn't installed → ImportError with
  remediation `pip install django-zed-rebac[strawberry]`.

---

## LocalBackend changes

`LocalBackend.write_relationships()` already populates `written_at_xid`
via `self._next_xid()`. `Zookie` returned from the write becomes the
freshness cutoff: when a subsequent read passes
`Consistency.at_least_as_fresh(zookie)`, the query adds:

```python
qs = qs.filter(written_at_xid__lte=int(zookie.token))
```

Reads with no consistency / `minimize_latency` skip the filter. Reads
with `fully_consistent` skip it too (we always read the latest in
LocalBackend's single-writer model — Postgres MVCC is the authority).

This is a **no-op for correctness in LocalBackend** but lets the
`SpiceDBBackend` honour the same `Zookie` shape transparently. The
public API contract holds across both.

---

## SpiceDBBackend changes

`SpiceDBBackend.write_relationships()` already receives a `ZedToken`
from gRPC. Wrap it as `Zookie(kind="spicedb", token=zedtoken.token)`.
Reads with `Consistency.at_least_as_fresh(zookie)` translate to
`at_least_as_fresh: {token: zookie.token}` in the protobuf
`Consistency` union. Reads with `Consistency.fully_consistent` go
through as-is.

The Zookie is opaque between backends — flipping `REBAC_BACKEND`
invalidates any cached Zookies. Document this in the settings catalog
("if you swap backends, drain any persisted Zookies; the kind prefix
guards against accidentally feeding a spicedb token to LocalBackend").

---

## Subscription lifecycle (the new bit)

Strawberry subscriptions over `graphql-transport-ws` protocol:

```
client → server: connect (WS upgrade)
   server: RebacChannelsConsumerMixin.connect — resolve actor from scope.user
           set _current_actor (connection-level, NOT per-emission)
client → server: subscribe (operation start)
   server: RebacExtension.on_operation enters
           opens evaluator_scope() + zookie_scope() — per-emission!
   subscription resolver yields event 1
   server emits event 1 to client
   RebacExtension.on_operation exits (per-yield)
client ← server: next (data emission)
   ...
   subscription resolver yields event 2
   server: RebacExtension.on_operation enters AGAIN
           fresh evaluator + fresh zookie scope
   ...
```

The key invariant: **`_current_actor` lives at connection scope; the
evaluator + zookie live at emission scope**. A long-lived subscription
that started 2h ago has the same actor identity as it does now (auth
re-validation is a separate concern), but no stale permission decisions.

For HTTP GraphQL (queries + mutations), the existing `ActorMiddleware`
brackets the request. The `RebacExtension` is still installed but its
scopes nest harmlessly inside the middleware's — ContextVar's natural
set/reset behaviour collapses the duplicate.

---

## Files to touch

| File | Change |
|---|---|
| `src/rebac/evaluator.py` | NEW — `PermissionEvaluator`, `current_evaluator`, `evaluator_scope` |
| `src/rebac/consistency.py` | NEW — `current_zookie`, `record_zookie`, `zookie_scope`, `effective_consistency` |
| `src/rebac/actors.py` | Migrate `accessible_cached` to delegate to evaluator; keep public signature |
| `src/rebac/types.py` | Add `Consistency.at_least_as_fresh` / `fully_consistent` / `minimize_latency` classmethods (if not present) |
| `src/rebac/middleware.py` | Replace `enable/disable_accessible_cache` with `evaluator_scope` + `zookie_scope`; add header rehydrate/persist |
| `src/rebac/relationships.py` | Call `record_zookie` after write/delete |
| `src/rebac/backends/local.py` | Honour `at_least_as_fresh` via `written_at_xid__lte` filter |
| `src/rebac/backends/spicedb.py` | Translate `Zookie` ↔ `ZedToken` (already partially there) |
| `src/rebac/__init__.py` | Export `PermissionEvaluator`, `current_evaluator`, `evaluator_scope`, `current_zookie`, `record_zookie`, `zookie_scope` |
| `src/rebac/conf.py` | New settings + check IDs E007, W006, W007 |
| `src/rebac/checks.py` | E007, W006 |
| `src/rebac/graphql/__init__.py` | NEW — package init |
| `src/rebac/graphql/strawberry.py` | NEW — `RebacExtension`, `RebacChannelsConsumerMixin` |
| `pyproject.toml` | `[project.optional-dependencies]` add `strawberry = ["strawberry-graphql>=0.220", "channels>=4.0"]` |
| `tests/test_evaluator.py` | NEW — cache hit/miss, eviction, scope nesting, async ctx isolation |
| `tests/test_consistency.py` | NEW — zookie record + read auto-upgrade, transport modes, kind-prefix guard |
| `tests/test_middleware_evaluator.py` | NEW — middleware brackets work + header transport round-trip |
| `tests/test_graphql_strawberry.py` | NEW — extension per-operation reset, subscription per-emission reset (mocked WS) |
| `tests/test_local_backend.py` | Add: `at_least_as_fresh` honours the xid cutoff |
| `README.md` | Highlights bullet: "GraphQL/WS-aware: per-emission permission cache + Zookie freshness propagation" |
| `docs/ARCHITECTURE.md` | New "Per-request evaluator" + "Freshness & Zookies" sections |
| `CHANGELOG.md` | 0.4 entry append |

---

## Test coverage required

1. **Evaluator cache hit/miss.**
   - Same `(subject, action, resource)` called twice → 1 backend call.
   - Different actions → 2 calls.
   - Different actors → 2 calls (cache is per-evaluator instance, but
     the key includes subject — still distinct).
2. **Cache eviction.**
   - Inserting `REBAC_EVALUATOR_CACHE_SIZE + 1` distinct keys evicts
     the LRU entry; the rest stay.
3. **Conditional results bypass cache.**
   - `CONDITIONAL_PERMISSION(missing=[...])` is never cached. The next
     call (with or without context) re-evaluates.
4. **Scope isolation across async tasks.**
   - Two `asyncio.create_task`-spawned coroutines each opening their
     own evaluator scope must see distinct caches. ContextVar's task
     copy-on-set semantics provide this; pin with a test.
5. **`accessible_cached` legacy delegation.**
   - Existing tests for `accessible_cached` must keep passing without
     modification. The signature is unchanged.
6. **Zookie record on write.**
   - `write_relationships(...)` populates `current_zookie()`.
   - `delete_relationships(...)` populates `current_zookie()` (delete
     is also a write — the new state matters for freshness).
7. **Zookie read auto-upgrade.**
   - After a write in scope, the next `accessible()` call without
     explicit consistency translates internally to
     `Consistency.at_least_as_fresh(zookie)`.
   - Explicit consistency wins — passing `Consistency.minimize_latency`
     explicitly suppresses the upgrade.
8. **LocalBackend `at_least_as_fresh` filter.**
   - Write a tuple at xid 100; record Zookie; concurrent write at xid
     200; read with `at_least_as_fresh(zookie at 100)` must NOT see
     the xid 200 row (LocalBackend's freshness is "rows with xid <=
     cutoff" — only the cutoff and earlier).
9. **Zookie kind-prefix guard.**
   - Backend rejects a Zookie whose `kind` doesn't match its own
     (`local` vs `spicedb`) with a clear error.
10. **Header transport round-trip.**
    - `REBAC_ZOOKIE_TRANSPORT="header"` — write request emits
      `X-Rebac-Zookie` response header; subsequent request with
      `X-Rebac-Zookie` request header populates `current_zookie()`
      before resolver runs.
11. **Session transport.**
    - `REBAC_ZOOKIE_TRANSPORT="session"` — write persists into
      `request.session[_rebac_zookie]`; next request rehydrates it.
12. **Strawberry extension per-operation reset.**
    - Two queries in one HTTP request (batched GraphQL) each get a
      fresh evaluator. Cache from query 1 does not bleed into query 2.
13. **Strawberry extension per-subscription-tick reset.**
    - Mock a subscription that yields 3 events. Permission revoked
      between event 1 and event 2 → event 2 must reflect the
      revocation. Test asserts the cache is reset between yields.
14. **Channels consumer actor resolution at handshake.**
    - `RebacChannelsConsumerMixin.connect()` populates `current_actor`
      from `scope["user"]`. Anonymous WS connection populates the
      anonymous actor.
15. **`[strawberry]` extra not installed.**
    - Importing `rebac.graphql.strawberry` without `strawberry-graphql`
      raises a clear `ImportError` mentioning the extra.
16. **System checks.**
    - E007 fires on invalid transport value.
    - W006 fires when transport=session but contrib.sessions missing.

---

## Out of scope — do NOT change

- `Backend` ABC signatures (`check_access`, `accessible`,
  `write_relationships`, `delete_relationships`, etc.).
- The schema parser, AST, `.zed` grammar.
- `rebac.roles` — already routes through `active_relationship_model()`
  and benefits from the evaluator automatically.
- The denormalized vs registry storage shape (proposal 0001).
- DataLoader integration in Strawberry — consumers can build their own
  with the evaluator as the dedup layer; not our concern.
- GraphQL federation auth — separate beast, defer.
- Async ORM support — still on the 0.5+ roadmap.
- DRF integration — `RebacPermission` and `RebacFilterBackend` already
  work; they'll see the evaluator transparently via the middleware
  bracket. Documentation pass only.
- Celery `propagate_actor` — already on the 0.3+ roadmap; this proposal
  doesn't block or accelerate it. When it lands, the Celery hook opens
  an `evaluator_scope` per task.

---

## Acceptance

- All existing 294 tests pass unchanged.
- New tests for evaluator + consistency + middleware bracket + Strawberry
  extension + Channels consumer mixin pass (~25 new tests).
- `mypy --strict src/rebac/evaluator.py src/rebac/consistency.py
  src/rebac/graphql/` reports no new errors.
- `pip install django-zed-rebac[strawberry]` succeeds; importing
  `rebac.graphql.strawberry` without the extra raises the expected
  `ImportError`.
- Determinism test for `build-zed` still byte-identical (no schema-emitter
  changes).
- README + ARCHITECTURE.md + CHANGELOG updated.
- An integration test in `tests/test_graphql_strawberry.py` shows a
  mock subscription with permission revocation between emissions
  flipping at the next yield.

---

## Rollout plan

- **0.4** (this proposal) ships the evaluator + Zookie ContextVar +
  Strawberry/Channels adapter behind `[strawberry]` extra. Default
  transport `"none"`; consumers using subscriptions opt in to
  `RebacChannelsConsumerMixin` + `RebacExtension`. Existing HTTP
  consumers see the per-request evaluator transparently — no API
  change.
- **0.4.x** — observe in the wild; document any subscription patterns
  that emerge.
- **0.5** — Zookie freshness becomes default-on for SpiceDB backend
  (the dispatcher-cache staleness is a real correctness gap there).
  LocalBackend continues to no-op the cutoff filter unless the operator
  opts in for cross-replica reads.
- **0.6** — DataLoader-style batch_check support if Strawberry +
  evaluator usage demands it.

---

## Context from prior work

Recent additions in 0.3.x–0.4 that touch overlapping files; the
proposal accounts for them:

- `src/rebac/actors.py` — `_accessible_cache` ContextVar +
  `accessible_cached` helper + `enable/disable_accessible_cache`
  middleware hooks. Proposal collapses these into the evaluator;
  signatures preserved.
- `src/rebac/middleware.py` — `ActorMiddleware` brackets `_current_actor`
  and the accessible cache; proposal extends with the Zookie bracket.
- `src/rebac/models/relationship.py` — registry model + manager
  (proposal 0001). The `at_least_as_fresh` filter applies to either
  the active model; the FK-side queryset translator passes
  `written_at_xid__lte` through unchanged.
- `src/rebac/roles.py` — already routes through
  `active_relationship_model()`; the evaluator caches the
  `accessible()` and `check_access()` calls that `roles_of` /
  `members_of` indirectly make. Test that role grants written in the
  same request are visible to subsequent role checks via Zookie
  freshness.

None of these conflict; pull from `main` before starting.

## Decisions locked (formerly open questions)

1. **`accessible_cached` is quietly deprecated.** Kept as a thin alias
   that delegates to `current_evaluator().accessible(...)` and emits a
   `DeprecationWarning` (stacklevel=2) on first call per process so
   downstream callers see the warning once without being spammed.
   Removed in 0.6 alongside the denormalized storage path. The
   `enable_accessible_cache` / `disable_accessible_cache` helpers
   become DeprecationWarning-emitting aliases for `evaluator_scope().__enter__`
   / `__exit__` so existing middleware-style call sites keep working.
2. **LRU eviction.** `REBAC_EVALUATOR_CACHE_SIZE` (default 10_000)
   bounds total entries across both check and accessible caches.
   Implementation uses `collections.OrderedDict` with `move_to_end` on
   hit and `popitem(last=False)` on overflow.
3. **Evaluator surfaced on `info.context.rebac_evaluator`.** The
   Strawberry extension mirrors `current_evaluator()` onto
   `info.context` so resolvers that prefer DI over ambient ContextVar
   get an explicit handle. Mirroring is best-effort — if the consumer's
   `info.context` is read-only the extension silently skips (logged at
   DEBUG).
4. **`RebacChannelsConsumerMixin` is a mixin.** No concrete consumer
   base shipped; downstream composes it with `JsonWebsocketConsumer` /
   `AsyncJsonWebsocketConsumer` / `GraphQLWSConsumer` as they prefer.
5. **Tests unit-mock the Strawberry Schema.** Fast pytest path uses a
   minimal `info` stub + extension-lifecycle assertion. One
   integration test behind `pytest -m strawberry_integration` (and
   the corresponding `[strawberry]` extra installed) exercises a real
   Strawberry schema + Channels test client; gated so the default CI
   matrix doesn't pay the Strawberry import cost.
