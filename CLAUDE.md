# CLAUDE.md

Guidance for Claude Code working in the `django-zed-rebac` repository.

> See `docs/SPEC.md` and `docs/ZED.md` for the design contract. Those docs are the source of truth.

---

## Project overview

`django-zed-rebac` is a **standalone, drop-in REBAC plugin for any Django 4.2 / 5.2 / 6.0 project**. SpiceDB-compatible schema language, two interchangeable backends (`LocalBackend` recursive-CTE in pure Django; `SpiceDBBackend` over `authzed-py`), strict-by-default queryset scoping, AI-agent Grant pattern, MCP / Celery / DRF / GraphQL adapters.

**Status: pre-alpha. Specs are settled; no Python code exists yet.** The repo currently contains only:

```
django-zed-rebac/
├── README.md         # Package pitch + quickstart + comparison
├── CLAUDE.md         # This file
└── docs/
    ├── SPEC.md       # Implementation specification (898 lines)
    └── ZED.md        # Schema authoring guide (974 lines)
```

When implementation begins, the package source will live at `src/zed_rebac/` (assuming PEP 621 src layout — see [§ Naming open question](#-naming-inconsistency-open-question) below).

---

## Documentation hierarchy

Read in this order before changing anything substantive:

| Doc | Purpose | When to read |
|---|---|---|
| `README.md` | Public-facing pitch. What the package is, why use it, comparison to alternatives. | Always — it's the elevator pitch. |
| `docs/SPEC.md` | Implementation specification. Architecture, public API, settings catalog, surface integrations (DRF/Celery/MCP/GraphQL), determinism guarantees, testing strategy, roadmap. | Before adding any code, changing a public API, or touching settings/migrations. |
| `docs/ZED.md` | User-facing schema authoring guide. Patterns library, anti-patterns, scenario walkthroughs (users/groups, agents, MCP tools, Celery tasks, etc.). | Before changing the schema-builder API, before touching how `Meta.permission_relations` is parsed, or when documenting a new authoring scenario. |

**If a behaviour isn't specified, propose a spec change first.** Don't implement undocumented behaviour and then patch the spec to match — that's how design intent erodes.

---

## Critical project invariants

These are non-negotiable. Violations break either the SpiceDB-compatibility contract or the security-critical strict-by-default posture.

### 1. SpiceDB compatibility is a public contract

The schema language, terminology (subjects / resources / relations / permissions / caveats / Zookies), and the `Backend` API surface mirror SpiceDB so that flipping `ZEDRBAC_BACKEND = "spicedb"` is a **configuration change, not a code change**.

- **Don't** add `LocalBackend`-only conveniences that have no SpiceDB equivalent (e.g., a "list all permissions for this user across all resource types" RPC SpiceDB doesn't expose).
- **Do** mirror `authzed.api.v1` method names in the Python `Backend` ABC (snake_case wrappers around `CheckPermission`, `LookupResources`, `LookupSubjects`, `WriteRelationships`, `WriteSchema`, `ExpandPermissionTree`, etc.).
- **Do** emit `use typechecking` at the top of every generated `.zed` file (catches mutually-exclusive-type intersections at WriteSchema time).

### 2. Strict-by-default is non-negotiable

A queryset that escapes its actor scope **must raise `MissingActorError`**, not silently return all rows. This is the django-scopes / django-tenants design that prevents the worst data-leakage bug class.

- `ZEDRBAC_STRICT_MODE = True` is the production default.
- Bypass requires explicit `.sudo(reason="...")` or `with system_context(reason="..."):`. Both write a structured audit-log event.
- Empty `sudo()` calls without `reason` raise when `ZEDRBAC_REQUIRE_SUDO_REASON = True` (default).

### 3. Three-state `CheckResult`

`Backend.check_permission()` returns one of `HAS_PERMISSION`, `NO_PERMISSION`, `CONDITIONAL_PERMISSION(missing=[...])` — **not a bare `bool`**. The `CONDITIONAL` state mirrors SpiceDB's behaviour when caveat context is partially supplied; callers may retry with additional context. Don't simplify to `bool` — that erases the layered-checks invariant.

### 4. Actor sticks to queryset / instance (generalised Odoo `with_user()` model)

The primary, generic verb is `.with_actor(actor)` where `actor` is any `ActorLike` — a Django `User`, a registered `Agent`, an `auth/grant` `SubjectRef`, an `auth/apikey` `SubjectRef`, or anything `@zed_subject`-decorated. Two typed shorthands cover the common cases:

- `.as_user(user)` — Django `User` shortcut (≡ `with_actor(to_subject_ref(user))`).
- `.as_agent(agent, on_behalf_of=user)` — agent acting via grant (≡ `with_actor(grant_subject_ref(agent, user))`).

When a queryset is created with any of the above, every instance loaded from it carries the resolved `SubjectRef` via `from_db()`. A subsequent `instance.save()` checks against that same actor, regardless of what `current_actor()` says now.

- **Don't** name a queryset method `as_user` and have the implementation be different from `with_actor(to_subject_ref(user))`. There is exactly one code path; the shorthands are sugar.
- **Don't** mutate a `ContextVar` inside `with_actor()`. The actor lives on the queryset instance.
- **Don't** re-resolve the actor on `save()` — that breaks the Celery / cron invariant where work continues under the original requester after their HTTP request ended.
- **Do** copy the actor into instances via `from_db` and into chained queryset clones via `_clone()` overrides.
- **Do** make per-queryset `.with_actor(...)` strictly higher-priority than `current_actor()` ContextVar. Ambient context never overrides explicit local scope.

### 4a. Sudo does NOT propagate through relationship traversal

The single largest deliberate divergence from Odoo's `env.su` semantics. In Odoo, `record.sudo().lines.user_id` reads BOTH `lines` AND `user_id` in sudo because `env` propagates through every traversal. We don't:

- `instance.sudo(reason=...)` flips the bypass for *this instance only*. Any FK accessor / reverse-FK manager / M2M / chained queryset on it re-resolves the actor against the carrying scope (`current_actor()` or the queryset's pinned actor) — it does NOT inherit the sudo flag.
- If you genuinely need related rows under sudo, the inner queryset must call `.sudo(reason="...")` again. The audit log records every bypass independently — that's the point.
- **Don't** add a "sudo flag propagates through `from_db`" shortcut. Transitive sudo is a contagion; cutting it at every relationship boundary forces each bypass to be greppable. See [SPEC.md § Lessons from Odoo 19](./docs/SPEC.md#lessons-from-odoo-19--footguns-we-avoid).

### 4b. No implicit "owner from `create_uid`"

Odoo's `ir.rule` filters routinely use `('create_uid', '=', user.id)` to derive ownership from the audit column. We don't. Ownership is an explicit `Relationship` row (`<resource>#owner @ auth/user:<id>`) written by your `post_save` signal handler or application code. The `created_by` / `created_at` audit columns on your model are independent.

- **Don't** add a "default owner = creator" path that derives ownership from a write column. Even if it seems convenient. Ownership must be transferable and revocable; conflating with audit makes both impossible without breaking audit.

### 4c. Soft-deleted rows participate in permission checks

Archived/inactive rows are visible to permission evaluation by default. Soft-delete is orthogonal to permission scope — an admin with `delete` on an archived resource needs to be able to un-archive it. If callers want to hide archived rows, they filter at the queryset level (`Post.objects.with_actor(u).filter(archived=False)`); the permission walk over `Relationship` does not exclude them.

- **Don't** introduce an `active_test`-style toggle (Odoo's per-call footgun) that flips visibility from inside the permission layer. It's a top-level policy.

### 5. Determinism is load-bearing

`python manage.py zedrbac build` must emit byte-identical `schema.zed` / `permissions.json` / `caveats.json` / `capabilities.json` across runs / machines / Python versions / Django versions.

- Sort definitions, relations, permissions, caveats — alphabetical by name.
- `json.dumps(..., sort_keys=True, indent=2, ensure_ascii=False)` + trailing `\n`.
- No timestamps, no `datetime.now()`, no `uuid4()`, no `random.*`.
- All set / dict iteration: `sorted()`. All filesystem walks: `sorted(os.listdir(...))`.
- The CI determinism test runs the build twice in a tmpdir and byte-diffs.

### 6. Operator precedence in permission expressions

SpiceDB's expression grammar binds `+` (union) tighter than `&` (intersection) tighter than `-` (exclusion). **Always emit explicit parentheses** in compound expressions. Single-line schema fragments without parens are a footgun even when they parse.

### 7. Wildcards only on read-shaped permissions

`auth/user:*` (or any `type:*` wildcard) **must not appear** in write/delete/create permissions. The schema-builder system check (`zedrbac.W001`-class) emits a warning at build time when a wildcard relation feeds a non-read permission. Don't suppress the check; fix the schema.

### 8. AI agents go through the Grant pattern

An agent acting on behalf of a user gets the **structural intersection** of (a) the user's grants and (b) the agent's declared capabilities — enforced by the `auth/grant#active` SubjectSet pattern in target-resource type unions. **Don't** model agents as direct REBAC principals (`relation viewer: auth/agent`) — that bypasses the user's grants entirely and is the canonical anti-pattern.

---

## Standalone-ness rule

This package **must work in any Django project without Angee**. Hard rule:

- **No `from angee.*` imports anywhere** in source, tests, examples, or docs.
- **No references to Angee patterns** in user-facing docs (`README.md`, `docs/`).
- **No `[tool.angee]`** or `[tool.zed_rebac]`-flavoured Angee config conventions in `pyproject.toml`.

Angee will eventually adopt `django-zed-rebac` as a dependency (see `../django-angee/specs/django-angee-auth/extract-django-zed.md`), but the relationship is one-way: **angee depends on zed-rebac, never the reverse**. The plugin is identity-agnostic by design — it knows nothing about Angee's `User` model, `RequestContext`, sqid IDs, or composition engine.

The only acceptable Angee-side adapter point is `ZEDRBAC_ACTOR_RESOLVER` — a dotted-path setting that lets a downstream package supply a custom `request → SubjectRef` resolver. Angee uses this; standalone consumers don't.

---

## ⚠ Naming inconsistency — open question

The directory was renamed from `django-zedrbac` to `django-zed-rebac`. The Python import name in the existing specs is `zedrbac` (no underscores, no hyphens). This is currently inconsistent.

**Options for the Python module name:**

1. `zed_rebac` — matches Django convention (hyphens → underscores). Examples: `django-rest-framework` → `rest_framework`, `django-celery-beat` → `django_celery_beat`. **Recommended.**
2. `zedrebac` — hyphens dropped entirely. Examples: `django-allauth` → `allauth`. Acceptable.
3. `zedrbac` — current spec text. Doesn't match any convention; would be confusing.

**Pip distribution name** is settled: `django-zed-rebac` (matches the directory).

**Action items before any code lands:**

1. Decide between `zed_rebac` (lean) and `zedrebac`.
2. Sweep `docs/SPEC.md` and `docs/ZED.md` to replace `zedrbac` → chosen name. Approximate locations (search `\bzedrbac\b`):
   - `from zedrbac import ...` examples (~30 occurrences across both docs)
   - Settings prefix `ZEDRBAC_*` (~25 occurrences) — **keep this**, it's the env-var prefix and conventional to be uppercase-no-underscores even when the import name has underscores; matches `DJANGO_*`, `DRF_*` patterns.
   - Management command `python manage.py zedrbac ...` (~10 occurrences) — **keep this** as `zedrbac`; this is the command label, not the module name. (Or sweep to `zedrebac` for consistency — design call.)
   - System check IDs (`zedrbac.E001`, `zedrbac.W001`) — same call as the command.
   - File paths under `zedrbac/` in the layout sketch — sweep to chosen module.
3. Update `README.md` `INSTALLED_APPS = [..., "zedrbac"]` → `[..., "zed_rebac"]` (or chosen name).

Until decided, **don't write any Python code that pins the import name**. Adding a placeholder `src/zed_rebac/__init__.py` is fine; rewriting docs to match is not — wait for the decision.

---

## What this package is NOT (drift signals)

Reject scope creep that turns this into anything other than a REBAC engine. These belong in OTHER packages, never here:

- **Not a User model.** Use `django.contrib.auth.models.User` or any `AUTH_USER_MODEL`. The spec says so explicitly; if a PR adds a `User` subclass, reject it.
- **Not an authentication system.** No login views, no password reset, no OAuth. `django-allauth` / `dj-rest-auth` / `django-otp` exist for that.
- **Not a session manager.** Django's session middleware is fine.
- **Not a multi-tenant database router.** `django-tenants` and `django-organizations` solve this. The plugin works inside whatever tenant scope the project provides.
- **Not an audit-log system.** v1 emits structured logs only. A future `django-zed-rebac-audit` package may add a queryable log; not in scope here.
- **Not a policy DSL** like Polar or Cedar. The schema language is SpiceDB's `.zed`, REBAC-first. ABAC fragments go through caveats.
- **Not a User permission UI.** Admins use Django admin or a downstream package; the override-layer admin in `zed_rebac.admin` is bounded to surfacing the existing schema, not a generic policy editor.

If a feature request blurs one of these lines, **the answer is "different package, not here"**.

---

## Implementation guidelines (when code lands)

### Tooling

Per `docs/SPEC.md § Testing` and the django-package research:

- **Build:** `setuptools` via `pyproject.toml` (PEP 621). Source layout: `src/zed_rebac/`.
- **Lint:** `ruff` with line-length 100 (or 88 per Black convention — pick one and stick).
- **Format:** `ruff format` (Black-compatible).
- **Type-check:** `mypy --strict` AND `pyright` — both must pass on CI. Ship `py.typed`.
- **Test:** `pytest` + `pytest-django` for integration; pure-Python `pytest` for unit. Cross-backend contract tests via `testcontainers-spicedb`, opt-in marker.
- **CI matrix:** Python 3.11/3.12/3.13/3.14 × Django 4.2/5.2/6.0 × DB (sqlite for unit, postgres-15/16 for integration).
- **DjangoVer** for releases: `<DJANGO_MAJOR>.<DJANGO_FEATURE>.<PACKAGE_VERSION>`.

### AppConfig

Follow `docs/SPEC.md § AppConfig and system checks` exactly:

```python
# zed_rebac/apps.py
class ZedRebacConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name              = "zed_rebac"  # or chosen module name
    verbose_name      = "ZED-REBAC"
    default           = True

    def ready(self):
        from . import signals    # noqa: F401
        from . import checks     # noqa: F401
```

**Two lines in `ready()`.** No queries. No model instantiation. No backend resolution at import time. Backend singleton is constructed lazily on first access via `zed_rebac.backend()`.

### Migrations

- `Relationship` indexes ship in `0001_initial.py` — never as a documentation step.
- Every `RunSQL` operation has `reverse_sql`.
- Use `swapper` if `ZEDRBAC_RELATIONSHIP_MODEL` is genuinely swappable; otherwise mark `class Meta: managed = True` (default) and ship the standard migration.
- Migrations must run on PostgreSQL 13+, MySQL 8+, SQLite (test only). The recursive-CTE syntax differs slightly across these — test all three in CI.

### Settings

- All settings prefixed `ZEDRBAC_`. **No nested dict** (defeats `SILENCED_SYSTEM_CHECKS`).
- Read via `app_settings` object exposed by `zed_rebac.conf`.
- Validation routed through Django's system checks framework (IDs `zedrbac.E001`, etc.). **Don't** raise at import time or in `ready()`.
- Production-only checks (`--deploy`) for things like `ZEDRBAC_SPICEDB_TLS = False`.

### Public API surface

Settled in `docs/SPEC.md § Public API surface`. Adding to it requires a spec update; removing from it is a breaking change requiring a major bump.

```python
# What's PUBLIC and semver-stable:
from zed_rebac import (
    ZedRBACMixin, ZedRBACManager, ZedRBACQuerySet,
    require_permission, zed_resource, zed_subject,
    schema as s,
    Backend, LocalBackend, SpiceDBBackend,
    CheckResult, Consistency, Zookie,
    ObjectRef, SubjectRef, ActorLike,
    PermissionDenied, MissingActorError, CaveatUnsupportedError,
    PermissionDepthExceeded, NoActorResolvedError,
    current_actor, set_current_actor, actor_context,
    sudo, system_context,
    to_subject_ref, grant_subject_ref,
    app_settings,
)
from zed_rebac.drf import ZedPermission, ZedFilterBackend
from zed_rebac.celery import propagate_actor
from zed_rebac.mcp import zed_mcp_tool
```

`with_actor` itself is a method on `ZedRBACManager` / `ZedRBACQuerySet`, not a top-level import. The top-level `actor_context(actor)` is the context-manager equivalent for non-queryset code paths.

Anything under `zed_rebac._internal.*` is private and may break in any minor release.

---

## Common pitfalls

### Don't import models in `apps.py` at module level
`AppRegistryNotReady`. Import inside `ready()` or inside the function that uses them.

### Don't query the database in `ready()`
Breaks `migrate`, `makemigrations`, test DB setup. Validation goes through system checks.

### Don't replace `_base_manager` with the scoped manager
Django uses `_base_manager` for FK reverse caching, M2M intermediate handling, etc. — these break if filtering applies. Install the scoped manager as `objects` (`_default_manager`); leave `_base_manager` unfiltered. This is why bare-string `prefetch_related("rel")` doesn't auto-scope and the spec requires the explicit `Prefetch(queryset=...)` form.

### Don't forget the operator-precedence parens
`a + b & c` ≠ `a + (b & c)`. Always emit parens in the schema-builder for compound expressions. The build's typecheck won't catch a precedence mistake — only logic bugs at runtime will.

### Don't break the `Relationship` shape
The fields `(resource_type, resource_id, relation, subject_type, subject_id, optional_subject_relation, caveat_name, caveat_context, expires_at, written_at_xid)` are wire-compatible with `authzed.api.v1.Relationship`. **Renames are breaking.** Adding a field is fine if it has a sane default.

### Don't ship a non-deterministic build
Single biggest CI hazard. Re-test determinism whenever you touch the emitter:

```bash
python manage.py zedrbac build
md5 zedrbac/schema.zed > /tmp/h1
rm zedrbac/schema.zed
python manage.py zedrbac build
md5 zedrbac/schema.zed > /tmp/h2
diff /tmp/h1 /tmp/h2 || echo "FAIL: non-deterministic"
```

Do this in CI, not just locally.

### Don't add Caveat-only ABAC features that don't translate to `.zed`
If you add a feature only the `LocalBackend` can do (e.g. a Python lambda evaluator), it breaks the SpiceDB swap promise. The contract: every feature must compile to a valid `.zed` schema accepted by SpiceDB's `WriteSchema`.

### Don't reach for `select_related` on RBAC models in v1
The plugin doesn't auto-scope SQL JOINs in v1 — the custom SQL compiler that does is reserved for v1.x (post-stable). For now, use `prefetch_related(Prefetch("rel", queryset=Related.objects.with_actor(actor)))` with the explicit queryset. The system check warns on bare-string `prefetch_related` against an RBAC model; treat it as a real warning.

---

## Relationship to django-angee

`django-angee` (at `/Users/alexis/Work/fyltr/django-angee`) is a separate framework that will eventually adopt `django-zed-rebac` as a runtime dependency. The integration point is one-way:

```
django-angee-auth  →  django-zed-rebac
       ↑                      ↑
       │ depends on            │ pure standalone, no Angee imports
```

The Angee-side glue (`AngeeModelRBAC` mixin, `RequestContext`, `auth/grant` schema fragment, sqid integration, MCP envelope parsing) lives in `django-angee-auth`, not here. Cross-coupling rules:

- **`django-zed-rebac` knows nothing about Angee.** Don't add Angee-aware code paths, even behind a feature flag.
- **`ZEDRBAC_ACTOR_RESOLVER` is the only adapter point.** Angee provides `"angee.auth.zed_glue.resolve_actor_with_grant"`; standalone consumers use the default.
- **Don't optimise this package for Angee's use case.** It's a generic Django REBAC plugin first; Angee is one of many consumers.

If a question arises about how Angee will use this package, see `../django-angee/specs/django-angee-auth/extract-django-zed.md` for the extraction proposal — that doc is the contract for the relationship.

---

## Workflow

When implementing or modifying the plugin:

1. **Read the relevant spec section.** `docs/SPEC.md` for behaviour; `docs/ZED.md` for user-facing schema authoring.
2. **One concern per PR.** A change that touches the manager AND the build emitter is two PRs.
3. **Update specs first if the change is structural.** A new public API needs a spec entry before code lands.
4. **Run the verification chain** before reporting complete:
   - `ruff check src/ tests/`
   - `ruff format --check src/ tests/`
   - `mypy --strict src/`
   - `pyright src/`
   - `pytest` (unit)
   - `pytest -m django_db` (integration)
   - `pytest -m spicedb` (when Docker available)
   - `python manage.py zedrbac build --check` (in the integration test project)
5. **Determinism test on every emitter touch.** See [§ Don't ship a non-deterministic build](#dont-ship-a-non-deterministic-build).
6. **No backwards-compat shims during 0.x.** Lockstep breaking changes are allowed pre-1.0; document them in `CHANGELOG.md`. After 1.0, follow [DjangoVer](https://www.b-list.org/weblog/2024/nov/18/djangover/).

---

## Open design questions tracked in specs

The spec calls out several open questions; don't re-decide them ad hoc:

- **`select_related` SQL compiler propagation** (v1.x).
- **Relationship table partitioning at scale** (post-1.0).
- **Async ORM support** (0.5+).
- **Override-layer precedence vs caveat CONDITIONAL** (security-fail-closed lean).
- **MCP standardised identity field** (watching upstream MCP spec).
- **GraphQL `ZedField` class vs decorator-only** (0.6, lean: decorator).
- **Standalone admin SPA package** (defer; gather feedback first).

If you encounter one of these in a PR, reference the spec entry and either resolve it (with a spec update) or note it as deferred. **Don't silently land a partial decision in code.**
