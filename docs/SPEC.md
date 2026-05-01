# `django-zedrbac` — Specification

> Last updated: 2026-05-01
> Status: **draft for review** — first public spec; no code merged yet.
> Audience: framework maintainers, Django integrators evaluating fit, contributors.
>
> Companion docs:
> - [ZED.md](./ZED.md) — schema authoring guide. How to define permissions for users, groups, MCP tools, agents, Celery tasks, and arbitrary entities.

---

## TL;DR

`django-zedrbac` is a **drop-in REBAC plugin** for any Django 4.2 / 5.2 / 6.0 project. Add it to `INSTALLED_APPS`, declare per-model permission schemas in Python, and every queryset, save, and method call is gated against the effective user — without rewriting your viewsets.

Core capabilities:

- **A SpiceDB-compatible schema language** authored in Python; emits a single byte-deterministic `.zed` file plus a compiled JSON manifest.
- **Two pluggable backends:**
  - `LocalBackend` — pure-Django evaluation via PostgreSQL/MySQL/SQLite recursive CTEs over a single `Relationship` table. Zero infrastructure.
  - `SpiceDBBackend` — wraps the official [`authzed`](https://pypi.org/project/authzed/) Python client. Production drop-in; same Python API.
- **A `ZedRBACMixin` model mixin** that, by inclusion, replaces `Manager.objects` with a permission-aware variant. Every read scopes to the effective user; every write checks before SQL is issued.
- **`Model.objects.as_user(user)` / `instance.as_user(user)` / `Model.objects.sudo()`** patterns. Idiomatic and explicit. No ambient global state surprises.
- **First-class extension points** for DRF (`ZedPermission`, `ZedFilterBackend`), Celery (`actor_id` propagation through task headers), MCP (decorator-time hook), GraphQL (resolver decorator), and arbitrary Python classes (`@zed_resource`).
- **Strict-by-default**: a queryset that escapes its actor scope raises `MissingActorError` rather than silently returning all rows. Bypass requires explicit `.sudo()` and a logged `reason`.
- **Designed-for-AI-agents**: the canonical Authzed *Grant* pattern is supported out of the box. An agent acting on behalf of a user receives the structural intersection of the user's grants and the agent's declared capabilities — enforced by the schema graph, not by app-layer ANDs.

What `django-zedrbac` deliberately does **not** ship: a `User` model, auth providers, login UI, or session handling. Those are orthogonal — use `django.contrib.auth` (default) or any of `django-allauth` / `dj-rest-auth` / your own.

This document specifies the implementation. For the schema language, examples per scenario, and the patterns library (RBAC, ABAC, agent grants, time-bound access), see [ZED.md](./ZED.md).

---

## Quickstart

Three steps, ~15 lines total.

### 1. Install and add to `INSTALLED_APPS`

```bash
pip install django-zedrbac
```

```python
# settings.py
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    # ...
    "zedrbac",
]

AUTHENTICATION_BACKENDS = [
    "zedrbac.backends.ZedRBACBackend",
    "django.contrib.auth.backends.ModelBackend",
]

ZEDRBAC_BACKEND = "local"   # or "spicedb"
```

### 2. Mix into your model

```python
# blog/models.py
from django.db import models
from zedrbac import ZedRBACMixin, schema as s

class Post(ZedRBACMixin, models.Model):
    title  = models.CharField(max_length=200)
    body   = models.TextField()
    author = models.ForeignKey("auth.User", on_delete=models.CASCADE)

    class Meta:
        permission_relations = [
            s.relation("owner",  to="auth/user"),
            s.relation("viewer", to=["auth/user", "auth/group#member", "auth/user:*"]),

            s.permission("read",  expr="owner + viewer"),
            s.permission("write", expr="owner"),
        ]
```

### 3. Build the schema and use the model

```bash
python manage.py migrate           # creates the Relationship table
python manage.py zedrbac build     # emits zedrbac/schema.zed + zedrbac/permissions.json
```

```python
# blog/views.py
def post_detail(request, pk):
    post = get_object_or_404(
        Post.objects.as_user(request.user),
        pk=pk,
    )
    return render(request, "post.html", {"post": post})
```

The same flow works in DRF, Celery tasks, MCP tools, and management commands. See [§ Surface integrations](#surface-integrations) for each.

---

## Conceptual model

`django-zedrbac` is a faithful Django port of [Google's Zanzibar paper](https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/) as implemented by [SpiceDB](https://github.com/authzed/spicedb). Five core concepts:

| Concept | What it is | Example |
|---|---|---|
| **Subject** | Who is acting. A typed reference: `subject_type:subject_id`. | `auth/user:42`, `auth/agent:claude_v3` |
| **Resource** | What is being acted upon. A typed reference. | `blog/post:99` |
| **Relation** | A typed link from a subject to a resource. Rows in the `Relationship` table. | `blog/post:99 #owner @ auth/user:42` |
| **Permission** | A computed expression over relations. Never stored, always evaluated. | `permission read = owner + viewer` |
| **Caveat** | A CEL expression evaluated at check time against runtime context. | `permission read = viewer with ip_in_cidr` |

The fundamental check operation: `check_permission(subject, permission, resource, context)` returns one of:

- `HAS_PERMISSION` — granted.
- `NO_PERMISSION` — denied.
- `CONDITIONAL_PERMISSION(missing=[...])` — the schema's caveats need context that wasn't supplied. The caller may retry with additional context.

This three-state result mirrors SpiceDB exactly and is critical for layered checks (e.g., a fast first-pass without context to confirm a relationship exists, then a second pass with context to evaluate caveats).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         your Django project                      │
│                                                                   │
│  views/    drf/    celery/    mcp/    graphql/    plain Python   │
│    │         │        │         │         │            │          │
│    └─────────┴────────┴─────────┴─────────┴────────────┘          │
│                          │                                        │
│              ZedRBACMixin / ZedPermission / @zed_resource         │
│                          │                                        │
│  ┌───────────────────────▼────────────────────────────────┐      │
│  │                  zedrbac.backends.Backend (ABC)         │      │
│  │   check_permission()  lookup_resources()  ...           │      │
│  └───────────────────────┬────────────────────────────────┘      │
│                          │                                        │
│            ┌─────────────┴────────────┐                          │
│            │                          │                          │
│  ┌─────────▼──────────┐    ┌──────────▼──────────────┐          │
│  │  LocalBackend      │    │  SpiceDBBackend          │          │
│  │  ─────────────     │    │  ──────────────          │          │
│  │  recursive CTE     │    │  authzed.api.v1.Client   │          │
│  │  on Relationship   │    │  → gRPC to spicedb       │          │
│  │  + cel-python      │    │                          │          │
│  └────────────────────┘    └──────────────────────────┘          │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

**Three layers, one boundary.** The schema (Python builder → `.zed` + JSON) is the contract. Both backends honour it. Application code never imports a backend directly — it goes through the `Backend` ABC instance resolved from `ZEDRBAC_BACKEND` at app-ready.

**Where each integration hooks:**

| Surface | Hook | What it does |
|---|---|---|
| Django ORM | `ZedRBACMixin` metaclass | Replaces `objects` with `ZedRBACManager`; wires pre-save / pre-delete signals; installs `from_db` actor propagation. |
| DRF | `ZedPermission` (BasePermission) + `ZedFilterBackend` (BaseFilterBackend) | Per-action permission check on viewsets; queryset filter on list endpoints. |
| Celery | `before_task_publish` + `task_prerun` signals | Injects `actor_id` into task headers on enqueue; restores into ContextVar on worker. |
| MCP (FastMCP / official SDK) | `@zed_resource` decorator at tool-registration time | Wraps the tool function; resolves actor from `ctx.request_context.meta`; checks `invoke` permission before body runs. |
| GraphQL (graphene / strawberry) | `@zed_resource` resolver decorator | Same pattern as MCP, applied at field resolution. |
| Plain Python | `@zed_resource(type=..., id_attr=...)` | Registers the class as a known resource type for explicit `check_permission()` calls. |

---

## Public API surface

```python
from zedrbac import (
    # Mixin and managers
    ZedRBACMixin, ZedRBACManager, ZedRBACQuerySet,

    # Decorators
    require_permission, zed_resource,

    # Schema authoring
    schema as s,

    # Backend interface (typically not used directly)
    Backend, LocalBackend, SpiceDBBackend,
    CheckResult, Consistency, Zookie,
    ObjectRef, SubjectRef, Relationship as RelationshipTuple,

    # Errors
    PermissionDenied, MissingActorError, CaveatUnsupportedError,
    PermissionDepthExceeded, NoActorResolvedError,

    # Actor resolution
    current_actor, set_current_actor, sudo, system_context,

    # Settings (advanced)
    app_settings,
)

from zedrbac.drf import ZedPermission, ZedFilterBackend
from zedrbac.celery import propagate_actor    # connect with @receiver
from zedrbac.mcp import zed_mcp_tool
```

Everything else (`zedrbac._internal.*`) is private and may change in any minor release.

---

## Models

Two concrete tables ship with the plugin. One is mandatory; one is optional and only relevant when admins need runtime override capability.

### `Relationship` — the core REBAC store

```python
# zedrbac/models.py (sketch)
class Relationship(models.Model):
    resource_type             = models.CharField(max_length=64, db_index=True)
    resource_id               = models.CharField(max_length=64, db_index=True)
    relation                  = models.CharField(max_length=64, db_index=True)
    subject_type              = models.CharField(max_length=64, db_index=True)
    subject_id                = models.CharField(max_length=64, db_index=True)
    optional_subject_relation = models.CharField(max_length=64, blank=True)
    caveat_name               = models.CharField(max_length=64, blank=True)
    caveat_context            = models.JSONField(null=True, blank=True)
    expires_at                = models.DateTimeField(null=True, blank=True, db_index=True)
    written_at_xid            = models.BigIntegerField(db_index=True)

    class Meta:
        indexes = [
            # Forward: "what subjects have <relation> on <resource>?"
            models.Index(fields=["resource_type", "resource_id", "relation"]),
            # Reverse: "what resources does <subject> have <relation> on?"
            models.Index(fields=["subject_type", "subject_id", "relation"]),
            # Subject-set traversal (group#member -> user)
            models.Index(fields=["subject_type", "subject_id", "optional_subject_relation"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "resource_type", "resource_id", "relation",
                    "subject_type", "subject_id", "optional_subject_relation",
                    "caveat_name",
                ],
                name="zedrbac_relationship_uniq",
            ),
        ]
```

**Frozen contract.** The shape mirrors `authzed.api.v1.Relationship` exactly. Renames are breaking. Indexes are critical (the recursive CTE walks them on every check) and ship in the initial migration — never as a documentation step.

**Swappability.** Projects that need to extend the model (audit FKs, multi-tenant prefix, etc.) declare a custom subclass and point `ZEDRBAC_RELATIONSHIP_MODEL = "myapp.MyRelationship"`. The plugin uses [`swapper`](https://pypi.org/project/swapper/) to keep migrations correct across this swap. Default behaviour: `swapper` returns the built-in `zedrbac.Relationship`.

**`written_at_xid` (Zookie equivalent).** Populated on save:
- PostgreSQL: `txid_current()` via a default expression.
- MySQL: monotonic timestamp (microsecond precision).
- SQLite: package-global `time.monotonic_ns()` counter (test-mode only — not for production).

`Zookie` consistency tokens encode `f"{backend_kind}.{xid}"`. Tokens are **not portable** across backends; if a project flips `ZEDRBAC_BACKEND` from `local` to `spicedb`, persisted Zookies in caches must be drained.

**`expires_at`.** Mirrors SpiceDB's [`use expiration`](https://authzed.com/docs/spicedb/concepts/schema#use-expiration) feature (GA in v1.40+). Expired rows are evaluated as absent at check time; a periodic GC task (`zedrbac.gc.expire_relationships`) deletes them every 5 minutes by default.

### `PermissionOverride` — optional runtime override layer

Ships disabled by default. Set `ZEDRBAC_OVERRIDE_LAYER = True` to enable.

```python
class PermissionOverride(models.Model):
    KIND_DISABLE_RULE     = "disable_rule"
    KIND_TIGHTEN          = "tighten"
    KIND_LOOSEN           = "loosen"
    KIND_GRANT_CONSTRAINT = "grant_constraint"

    kind        = models.CharField(max_length=32, choices=[...])
    target      = models.CharField(max_length=128)              # "blog/post.read"
    expression  = models.TextField()                             # zed-syntax override
    reason      = models.TextField()                             # required; in audit log
    created_by  = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    no_update   = models.BooleanField(default=True)              # Odoo's noupdate semantic
```

Layered on top of the code baseline at app-ready. Mirrors Odoo's `ir.rule` runtime-tweak semantics with `noupdate=True` defaulting to "preserve admin edits across `zedrbac build`". An admin UI ships in `zedrbac.admin`. Every change writes a structured audit-log event.

---

## Settings catalog

All settings prefixed `ZEDRBAC_`. No nested `ZEDRBAC = {...}` dict (defeats `SILENCED_SYSTEM_CHECKS` and IDE autocomplete). Read via the public `app_settings` object; never via `getattr(django_settings, ...)` directly.

| Setting | Default | Type | Purpose |
|---|---|---|---|
| `ZEDRBAC_BACKEND` | `"local"` | `"local"` \| `"spicedb"` | Which backend to instantiate at app-ready. |
| `ZEDRBAC_RELATIONSHIP_MODEL` | `"zedrbac.Relationship"` | `str` | Swappable relationship model (Django convention). |
| `ZEDRBAC_SPICEDB_ENDPOINT` | `None` | `str` \| `None` | `host:port` for `authzed.api.v1.Client`. Required when backend is `spicedb`. |
| `ZEDRBAC_SPICEDB_TOKEN` | `None` | `str` \| `None` | Preshared key. Required when backend is `spicedb`. |
| `ZEDRBAC_SPICEDB_TLS` | `True` | `bool` | If `False`, uses `InsecureClient` (dev only). |
| `ZEDRBAC_SPICEDB_AUTO_WRITE_SCHEMA` | `True` | `bool` | On app-ready (when backend is `spicedb`), push the compiled schema via `WriteSchema`. |
| `ZEDRBAC_SCHEMA_DIR` | `BASE_DIR / "zedrbac"` | `Path` \| `str` | Where `zedrbac build` writes `schema.zed`, `permissions.json`, etc. |
| `ZEDRBAC_DEPTH_LIMIT` | `8` | `int` | Hard cap on recursive permission walks. Matches SpiceDB default. |
| `ZEDRBAC_DEFAULT_CONSISTENCY` | `"minimize_latency"` | `str` | Default `Consistency` for checks. |
| `ZEDRBAC_CACHE_ALIAS` | `"default"` | `str` | Django cache backend name for `lookup_resources` cache. |
| `ZEDRBAC_LOOKUP_CACHE_TTL` | `60` (s) | `int` | TTL for `lookup_resources` cache entries. Invalidated on relationship writes for the matching `(subject, action, resource_type)`. |
| `ZEDRBAC_PK_IN_THRESHOLD` | `10000` | `int` | Above this size, `lookup_resources` returns a JOIN instead of materialising `pk__in`. |
| `ZEDRBAC_STRICT_MODE` | `True` | `bool` | If `True`, queryset construction without an actor (and not in `sudo()`) raises `MissingActorError`. **Production default.** |
| `ZEDRBAC_REQUIRE_SUDO_REASON` | `True` | `bool` | If `True`, `sudo()` calls without a `reason=...` raise. |
| `ZEDRBAC_ALLOW_SUDO` | `True` | `bool` | Globally disable `sudo()`. Strict tenants set `False`. |
| `ZEDRBAC_OVERRIDE_LAYER` | `False` | `bool` | If `True`, `PermissionOverride` rows are loaded at app-ready. |
| `ZEDRBAC_GC_INTERVAL_SECONDS` | `300` | `int` | How often the expiration GC task runs. |
| `ZEDRBAC_ACTOR_RESOLVER` | `"zedrbac.actors.default_resolver"` | `str` | Dotted-path callable that resolves `request → SubjectRef`. Override for custom identity layers (e.g., agent grants). |
| `ZEDRBAC_TYPE_PREFIX` | `""` | `str` | Optional prefix for all generated resource types (multi-tenant SaaS). |

Validation runs in the system-checks framework at every `manage.py` invocation. Missing required keys for the chosen backend raise `Error` with check ID `zedrbac.E001`. Wrong types raise `zedrbac.E002`. Production-only checks (`--deploy`) include `zedrbac.W101` for `SPICEDB_TLS = False`.

---

## AppConfig and system checks

`apps.py` is exactly:

```python
# zedrbac/apps.py
from django.apps import AppConfig

class ZedRBACConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name              = "zedrbac"
    verbose_name      = "ZED-RBAC"
    default           = True

    def ready(self):
        from . import signals    # noqa: F401  — connects pre/post save handlers
        from . import checks     # noqa: F401  — registers system checks
```

**No queries. No model instantiation. No backend resolution at import time.** The backend singleton is constructed lazily on first access via `zedrbac.backend()` — this avoids `AppRegistryNotReady` and keeps `migrate` fast.

System checks (in `zedrbac/checks.py`):

| ID | Severity | What it validates |
|---|---|---|
| `zedrbac.E001` | Error | `ZEDRBAC_BACKEND` is `"local"` or `"spicedb"`. |
| `zedrbac.E002` | Error | Required SpiceDB settings present when backend is `spicedb`. |
| `zedrbac.E003` | Error | `Meta.permission_relations` references no undeclared types. |
| `zedrbac.E004` | Error | Permission expressions parse against operator grammar. |
| `zedrbac.W001` | Warning | `zedrbac.backends.ZedRBACBackend` not in `AUTHENTICATION_BACKENDS`. |
| `zedrbac.W002` | Warning | A model with `permission_relations` is missing `ZedRBACMixin`. |
| `zedrbac.W003` | Warning | A `prefetch_related("rel")` string-form for an `AngeeModelRBAC`-flagged related model — should use explicit `Prefetch(...)`. |
| `zedrbac.W101` | Warning (`--deploy`) | `ZEDRBAC_SPICEDB_TLS = False` in production. |

Users silence individual checks via Django's `SILENCED_SYSTEM_CHECKS = ["zedrbac.W001"]`.

---

## Authorization backend

```python
# zedrbac/backends.py
class ZedRBACBackend:
    """
    Django auth backend. Routes per-object has_perm() through the REBAC engine.
    Does not authenticate (returns None from authenticate()).
    """

    def authenticate(self, request, **credentials):
        return None  # let downstream backends authenticate

    def has_perm(self, user_obj, perm: str, obj=None) -> bool:
        if not getattr(user_obj, "is_active", False):
            return False
        if obj is None:
            return False  # let ModelBackend handle model-level checks
        from . import backend
        from .actors import to_subject_ref
        from .resources import to_object_ref

        rebac_action = _codename_to_action(perm)         # "blog.view_post" → "read"
        if rebac_action is None:
            return False

        return backend().check_permission(
            subject    = to_subject_ref(user_obj),
            permission = rebac_action,
            resource   = to_object_ref(obj),
        ).allowed

    def has_module_perms(self, user_obj, app_label):
        return False
```

**Codename mapping.** Default mappings (`{view_, change_, delete_, add_}_<model>` → `{read, write, delete, create}`) ship in `zedrbac.codenames`. Per-package overrides via:

```python
# yourapp/apps.py
class YourAppConfig(AppConfig):
    zedrbac_codename_map = {
        "yourapp.share_post":  "share",
        "yourapp.archive_post": "archive",
    }
```

The plugin's system check walks installed apps and merges their maps at app-ready.

**Superuser bypass.** Preserved by default for operational ergonomics: `is_superuser=True` short-circuits `has_perm` to `True`. Toggle via `ZEDRBAC_SUPERUSER_BYPASS = False` for strict tenants.

---

## `ZedRBACMixin` — model-layer enforcement

The headline feature. By inclusion, every model operation is gated against the effective user.

### What gets installed

1. `objects = ZedRBACManager.from_queryset(ZedRBACQuerySet)()` replaces the default manager.
2. `_default_manager` points at it; `_base_manager` is intentionally **left unfiltered** (Django uses `_base_manager` for its own internal lookups — FK reverse caching, M2M intermediate tables — and these break if filtering is applied there).
3. Pre-save signal handler — `write` permission check before INSERT/UPDATE.
4. Pre-delete signal handler — `delete` permission check.
5. `from_db()` override — every loaded instance carries the actor that scoped its queryset.
6. `Meta` extension — the metaclass parses `permission_relations` and validates against the schema language.

### Manager and queryset surface

```python
class ZedRBACManager:
    def get_queryset(self) -> ZedRBACQuerySet: ...
    def as_user(self, user) -> ZedRBACQuerySet: ...
    def as_subject(self, subject: SubjectRef) -> ZedRBACQuerySet: ...
    def sudo(self, *, reason: str = None) -> ZedRBACQuerySet: ...
    def unrestricted(self, *, reason: str = None) -> ZedRBACQuerySet: ...   # alias
    def actor(self) -> SubjectRef | None: ...                                # introspection

class ZedRBACQuerySet:
    # All the as_user variants; chainable.
    def as_user(self, user) -> Self: ...
    def as_subject(self, subject: SubjectRef) -> Self: ...
    def sudo(self, *, reason: str) -> Self: ...
    def unrestricted(self, *, reason: str) -> Self: ...

    # Standard queryset ops with REBAC-aware overrides:
    def update(self, **kwargs) -> int: ...
    def delete(self) -> tuple[int, dict]: ...
    def bulk_create(self, objs, **opts): ...
    def bulk_update(self, objs, fields, **opts): ...
    def create(self, **kwargs): ...
```

### Three actor-resolution paths

The manager picks the effective actor in this priority order, every time it materialises a queryset:

1. **Per-queryset actor**, set via `.as_user(user)` / `.as_subject(subject)`. Stored on the queryset instance — not on a ContextVar — so it survives chaining and DOESN'T leak across queryset boundaries.
2. **Per-queryset sudo**, set via `.sudo(reason=...)`. Bypasses scoping; logs a structured audit event.
3. **Implicit from `current_actor()`**, the contextvar populated by middleware (see [§ Middleware](#middleware)) and by Celery prerun hooks.
4. **Falls through to** `ZEDRBAC_STRICT_MODE` handling: `True` → raise `MissingActorError`; `False` → resolve to `system_context()` (full visibility).

**Critical: scope sticks across writes.** A queryset created with `as_user(u)` produces instances tagged with that actor; `instance.save()` re-checks against the same `u`, regardless of what `current_actor()` says now.

```python
# Inside a Celery task that loads as_user(u) and later modifies:
@shared_task
def archive_old_posts(user_id: int):
    user = User.objects.get(pk=user_id)
    for post in Post.objects.as_user(user).filter(created__lt=cutoff):
        post.archived = True
        post.save()        # checks "write" against u, even though current_actor() may differ
```

This is the Odoo `with_user(...)` invariant translated into Django — the actor follows the recordset.

### Instance-level helpers

```python
post = Post.objects.as_user(alice).get(pk=1)

# Explicit actor reassignment on a single instance:
post_as_bob = post.as_user(bob)
post_as_bob.save()                    # checks "write" against bob

# Bypass for a single op:
with post.sudo(reason="cron.normalize_titles"):
    post.title = post.title.strip()
    post.save()
```

Both `instance.as_user(user)` and `instance.sudo(...)` return a new instance reference (the original is untouched). Internally this is a shallow attribute copy with the actor slot overwritten.

### CRUD enforcement matrix

| Operation | Permission checked | Where |
|---|---|---|
| `Model.objects.all()` / `.filter(...)` / `.get()` / `.count()` / `.exists()` | `read` (or `Meta.zed_default_action`) on each candidate row. | `ZedRBACQuerySet.get_queryset()` injects a `pk__in=<lookup_resources(actor, action, type)>` clause (or a JOIN above the threshold). |
| `Model.objects.create(**fields)` | `create` on the model class | `ZedRBACManager.create()` calls `check_permission(actor, "create", ObjectRef(type, ""))` first. |
| `Model.objects.bulk_create(rows)` | `create` once per page | Single class-level check. |
| `instance.save()` (PK present) | `write` on the row | Pre-save signal handler. |
| `instance.save()` (new instance) | `create` on the model class | Pre-save handler dispatches based on `_state.adding`. |
| `instance.delete()` | `delete` on the row | Pre-delete signal handler. |
| `Model.objects.update(**fields)` | `write` on each affected row | Manager intersects the queryset PK set with `lookup_resources(actor, "write", type)`; raises `PermissionDenied` if any in-scope row is excluded by the write check. |
| `Model.objects.delete()` | `delete` on each row | Same pattern. |

**Failure mode for writes:** *all-or-nothing*. Any denied row in a bulk write raises and rolls back. Reasoning: silently skipping denied rows is more dangerous than full denial.

**Failure mode for reads:** denied rows are absent from the queryset. No raise. Reasoning: list endpoints return `[]` rather than 403 when the user has no rows.

### Cross-relation propagation

| Mechanism | v1 status | Behaviour |
|---|---|---|
| `prefetch_related(Prefetch("rel", queryset=Related.objects.as_user(u)))` | ✅ Required for RBAC-protected related models | Explicit and clean. The system check `zedrbac.W003` flags bare-string `prefetch_related("rel")` against an RBAC model. |
| `prefetch_related("rel")` (string form) | ⚠️ Warns in v1, errors in v1.x | Goes through `_base_manager` (unfiltered). Doctor + linter detect. |
| Reverse FK accessors (`instance.related_set.all()`) | ✅ Auto-propagates | Reuses the related model's `_default_manager` (which is RBAC-aware) and inherits the parent instance's actor via `from_db`. |
| Forward FK lazy-loads (`instance.author`) | ✅ Auto-propagates | Same mechanism. |
| `select_related("rel")` | ❌ NOT scoped in v1 | SQL JOINs cannot be filtered per-table via the public ORM API. Documented gap. |
| `select_related` propagation | 📋 v1.x | Custom `RBACSQLCompiler` walks `query.alias_map` and injects per-alias scope subqueries. Maintenance cost: Django version CI matrix. |

### `@require_permission` decorator for methods

```python
class Post(ZedRBACMixin, models.Model):
    @require_permission("write")
    def archive(self):
        self.archived_at = now()
        self.save()

    @require_permission("share", subject="acted_by")
    def share_with(self, target_user, *, acted_by=None):
        ...
```

- Default: resolves actor via the same priority order as the manager.
- `subject="kw_name"` reads the named keyword argument (must be a `User` or `SubjectRef`).
- `check_self=False`: checks against the model class instead of the row.

Decorated methods write a structured `zedrbac.method_call` audit event whether allowed or denied.

---

## Middleware

```python
# zedrbac/middleware.py
class ActorMiddleware:
    """
    Reads request.user (and optionally an X-Actor-Subject header) and
    populates a ContextVar consulted by ZedRBACManager when no explicit
    .as_user()/.sudo() is set on the queryset.
    """
    def __init__(self, get_response): ...

    def __call__(self, request):
        from .actors import set_current_actor, clear_current_actor

        actor = self._resolve(request)
        token = set_current_actor(actor)
        try:
            return self.get_response(request)
        finally:
            clear_current_actor(token)
```

Add to `MIDDLEWARE` after `AuthenticationMiddleware`:

```python
MIDDLEWARE = [
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "zedrbac.middleware.ActorMiddleware",
    # ...
]
```

The contextvar is exposed as `current_actor()` — works in async views, sync views, ASGI consumers, and DRF viewsets identically.

For non-request contexts (Celery, cron, management commands), use `with sudo(reason=...)` or set the actor explicitly via `.as_user(...)`.

---

## Surface integrations

### DRF

```python
# zedrbac/drf.py
class ZedPermission(BasePermission):
    """
    Default action map: list/retrieve→read, create→create,
    update/partial_update→write, destroy→delete.
    """
    action_map = {
        "list":           "read",
        "retrieve":       "read",
        "create":         "create",
        "update":         "write",
        "partial_update": "write",
        "destroy":        "delete",
    }

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False
        action = view.action
        if action == "create":
            from . import backend
            return backend().check_permission(
                subject    = to_subject_ref(request.user),
                permission = "create",
                resource   = ObjectRef(view.queryset.model._meta.label_lower, ""),
            ).allowed
        return True  # defer to has_object_permission + filter backend

    def has_object_permission(self, request, view, obj) -> bool:
        action = view.action
        perm   = self.action_map.get(action, "read")
        from . import backend
        return backend().check_permission(
            subject    = to_subject_ref(request.user),
            permission = perm,
            resource   = to_object_ref(obj),
        ).allowed


class ZedFilterBackend(BaseFilterBackend):
    """
    Filters list-view querysets to objects the actor has 'read' on.
    """
    def filter_queryset(self, request, queryset, view):
        return queryset.as_user(request.user)
```

Usage:

```python
class PostViewSet(viewsets.ModelViewSet):
    queryset           = Post.objects.all()
    serializer_class   = PostSerializer
    permission_classes = [ZedPermission]
    filter_backends    = [ZedFilterBackend]
```

For drf-spectacular OpenAPI emission, the optional `zedrbac.drf.spectacular` integration adds a security requirement to operations that include `ZedPermission`. Activated automatically if `drf_spectacular` is installed; no config needed.

### Celery

The full pattern uses two signals: `before_task_publish` (producer-side) and `task_prerun` (worker-side):

```python
# zedrbac/celery.py
from celery.signals import before_task_publish, task_prerun, task_postrun
from .actors import current_actor, set_current_actor, clear_current_actor

@before_task_publish.connect
def _inject_actor(headers=None, **kwargs):
    actor = current_actor()
    if actor is not None and headers is not None:
        headers["zedrbac_actor"] = actor.serialize()

@task_prerun.connect
def _restore_actor(task=None, **kwargs):
    serialized = (task.request.headers or {}).get("zedrbac_actor")
    if serialized:
        actor = SubjectRef.deserialize(serialized)
        task._zed_actor_token = set_current_actor(actor)

@task_postrun.connect
def _clear_actor(task=None, **kwargs):
    token = getattr(task, "_zed_actor_token", None)
    if token is not None:
        clear_current_actor(token)
```

**Wired automatically** by `zedrbac.apps.ZedRBACConfig.ready()` — no per-project setup. Inside a `@shared_task`, calling `Post.objects.as_user(...)` is unnecessary; the manager picks up the actor from the contextvar that `task_prerun` set:

```python
@shared_task
def email_user_their_drafts(user_id: int):
    # current_actor() is already populated from task headers
    drafts = Post.objects.filter(status="draft")   # scoped automatically
    send_email(drafts)
```

To run a task without an actor (cron, system maintenance), wrap in `with sudo(reason=...)` at the call site and the `before_task_publish` signal injects a sudo flag instead.

**Eager-mode caveat.** `before_task_publish` does NOT fire when `CELERY_TASK_ALWAYS_EAGER = True`. The plugin handles this by falling back to `task_prerun` — which DOES fire in eager mode — reading `current_actor()` directly. Tests work; production works.

### MCP (FastMCP / official Python SDK)

```python
# zedrbac/mcp.py
def zed_mcp_tool(*, resource_type: str, action: str = "invoke", id_arg: str | None = None):
    """
    Decorator wrapping FastMCP / official-SDK tool functions.
    Resolves actor from ctx.request_context.meta["actor_subject"];
    checks `action` permission against the tool's resource type before
    the tool body runs.
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, ctx, **kwargs):
            actor = _actor_from_mcp_context(ctx)
            resource_id = kwargs.get(id_arg, "") if id_arg else ""
            from . import backend
            result = backend().check_permission(
                subject    = actor,
                permission = action,
                resource   = ObjectRef(resource_type, resource_id),
            )
            if not result.allowed:
                raise PermissionDenied(...)
            return await fn(*args, ctx=ctx, **kwargs)
        return wrapper
    return deco
```

Usage with FastMCP:

```python
@mcp.tool
@zed_mcp_tool(resource_type="blog/post", action="write", id_arg="post_id")
async def edit_post(post_id: str, body: str, ctx: Context = CurrentContext()) -> dict:
    post = await Post.objects.aget(public_id=post_id)
    post.body = body
    await post.asave()                # save() goes through the same actor; passes
    return {"ok": True}
```

The MCP client must place the actor's subject into the request envelope's `meta` dict (`{"actor_subject": "auth/user:42"}`). The plugin does NOT mint or validate identity — that's the MCP server's transport-layer job (typically OAuth 2.1, per the MCP spec).

### GraphQL (graphene / strawberry)

```python
@zed_resource(type="blog/post", id_attr="pk")
@strawberry.type
class PostType:
    @strawberry.field
    @require_permission("read")
    def body(self, info) -> str: ...
```

Same pattern as MCP — resolver decorator wraps the field.

### Plain Python entities

```python
# A non-Django entity with a permission boundary
@zed_resource(type="storage/s3_prefix", id_attr="prefix")
class S3Prefix:
    def __init__(self, prefix: str):
        self.prefix = prefix

# Manual check elsewhere:
from zedrbac import backend, ObjectRef
backend().check_permission(
    subject    = to_subject_ref(user),
    permission = "read",
    resource   = ObjectRef("storage/s3_prefix", prefix),
)
```

The `@zed_resource` decorator registers the type with the schema validator (the build emits `definition storage/s3_prefix {}` so other models can declare relationships pointing at it).

---

## Granular reusable parts

`django-zedrbac` is built on small, composable primitives. Projects that need a piece — but not the whole `ZedRBACMixin` — can wire them directly:

| Part | What you import | When to use |
|---|---|---|
| `Backend` ABC + `LocalBackend` | `from zedrbac import LocalBackend, ObjectRef, SubjectRef` | You want REBAC checks in code without touching ORM. |
| `ZedRBACManager` standalone | `Model.objects = ZedRBACManager.from_queryset(ZedRBACQuerySet)()` | Drop scoping into a model without the metaclass. |
| `as_user_queryset(qs, user)` | `from zedrbac.querysets import as_user_queryset` | Apply scoping ad-hoc to any queryset. |
| `check_permission(subject, perm, resource)` | `from zedrbac import backend; backend().check_permission(...)` | Imperative checks anywhere. |
| `@require_permission` decorator | `from zedrbac import require_permission` | Gate methods on plain Python classes (not just models). |
| `current_actor()` ContextVar | `from zedrbac import current_actor` | Read the active actor inside any code path. |
| `with sudo(reason=...)` | `from zedrbac import sudo` | Block-scoped bypass; logged. |
| `with system_context(reason=...)` | `from zedrbac import system_context` | Like `sudo()`, idiomatic for cron. |
| Schema builder | `from zedrbac import schema as s` | Author schema fragments outside of `Meta`. |
| `BackendPermission` checker | `from zedrbac.backends import to_subject_ref` | Identity-to-subject conversion (extension point). |

---

## Management commands

```bash
python manage.py zedrbac build              # emit schema.zed + permissions.json + capabilities.json
python manage.py zedrbac build --check      # CI gate: regenerate to a buffer and diff against on-disk
python manage.py zedrbac build --explain Model      # print the compiled permission expression for Model
python manage.py zedrbac doctor             # validate codename maps, permission_relations, override layer
python manage.py zedrbac write-schema       # push current schema to SpiceDB (via WriteSchema RPC)
python manage.py zedrbac gc-expired         # one-shot run of the relationship expiration GC
```

`build --check` is the CI gate. It returns non-zero on drift and prints a unified diff. CI integrators add it as a pre-merge step alongside `migrate --check`.

---

## Determinism

Build output (`schema.zed`, `permissions.json`, `capabilities.json`, `caveats.json`) is **byte-identical** across runs, machines, Python versions, and Django versions. Conventions enforced by the build:

1. **Definition order**: alphabetical by name. NOT insertion order (depends on app loading sequence, which varies).
2. **Relations / permissions / caveats within a definition**: alphabetical by name.
3. **JSON outputs**: `json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False)` + trailing `\n`.
4. **No timestamps in generated files.** A content hash is computed from the *sorted, canonical inputs* (model names + field defs) and emitted as a comment header — deterministic by construction.
5. **All set / dict iteration: `sorted(...)`.** Filesystem walks: `sorted(os.listdir(...))`.
6. **Generator-version stamp**: `// Generated by django-zedrbac 1.0.0` — pinned to the installed version, not a `__version__` lookup at runtime.

The CI determinism test: run `zedrbac build` twice in a tmpdir, byte-diff. Failure on any difference. Mirrors the `manage.py makemigrations --check` pattern.

---

## Migration safety

| Risk | Mitigation |
|---|---|
| Initial install creates a `Relationship` table with billions of rows expected | Indexes shipped in `0001_initial.py`. Migration is idempotent. |
| Project running `--backwards` to before `zedrbac` was installed | Every `RunSQL` operation has `reverse_sql`. The full schema is reversible. |
| Swappable `Relationship` model adopted post-install | `swapper.dependency()` already wired in shipped migrations. New custom model gets a fresh migration that the project author writes. |
| Adding `expires_at` later (back-port to existing relationships) | `expires_at` is nullable; existing rows get `NULL`. No data migration needed. |
| Multi-tenant prefix added later (`ZEDRBAC_TYPE_PREFIX`) | Changing the prefix requires `manage.py zedrbac retype-relationships --from=... --to=...`. Documented prominently. |

---

## Testing

Three layers of tests ship in CI:

1. **Unit tests** (`pytest`): pure-Python, no database. Schema parsing, expression compilation, codename mapping, build determinism.
2. **Integration tests** (`pytest-django`, `@pytest.mark.django_db`): in-memory SQLite + real Postgres. `ZedRBACMixin` end-to-end, manager scoping, signal handlers.
3. **Cross-backend contract tests**: same suite parameterised over `LocalBackend` and `SpiceDBBackend` (the latter via [`testcontainers-spicedb`](https://pypi.org/project/testcontainers-spicedb/)). Run on CI when Docker is available; opt-in via `pytest -m spicedb`.

CI matrix:

```
Python:  3.11 · 3.12 · 3.13 · 3.14
Django:  4.2 · 5.2 · 6.0
DB:      sqlite (unit) · postgres-15 (integration) · postgres-16 (integration)
Backend: local · spicedb (when Docker available)
```

Type-checked end-to-end: `mypy --strict src/zedrbac/` and `pyright --pythonversion 3.13`. Both run on CI; both must pass. The plugin ships `py.typed` (PEP 561) and `ZedRBACManager[M]` is `Generic[M]` over the model class so query return types are inferred.

---

## Versioning

`django-zedrbac` follows [DjangoVer](https://www.b-list.org/weblog/2024/nov/18/djangover/): `<DJANGO_MAJOR>.<DJANGO_FEATURE>.<PACKAGE_VERSION>`. Examples:

- `6.0.1` — works with Django 6.0, package iteration 1.
- `5.2.4` — works with Django 5.2 LTS, fourth iteration.
- `5.2.4 → 6.0.1` is the supported upgrade path; we ship it the day Django 6.0 lands.

LTS support: Django 4.2 LTS through April 2026, Django 5.2 LTS through April 2028. We track Django's own deprecation policy and never force users off LTS prematurely.

Public API (`zedrbac.*` direct imports + the `Meta.permission_relations` shape) is semver-stable across same-Django versions. `zedrbac._internal.*` is private. Breaking changes are confined to Django-major bumps.

---

## Roadmap

| Phase | Deliverable |
|---|---|
| **0.1.0 — MVP** | `LocalBackend` (Postgres CTE, MySQL CTE, SQLite test-mode); schema builder + `.zed` emit; `ZedRBACMixin` + manager + signals; `ZedPermission` + `ZedFilterBackend`; system checks; build/check/doctor commands; full test matrix. |
| **0.2.0 — Caveats + expiration** | `cel-python` integration; `use expiration` schema directive; expiration GC task. |
| **0.3.0 — Celery + middleware** | `ActorMiddleware`; Celery signal handlers; ContextVar stack pattern. |
| **0.4.0 — Override layer** | `PermissionOverride` model + admin; runtime tweak surface. |
| **0.5.0 — `SpiceDBBackend`** | `authzed-py` adapter; `WriteSchema` auto-push; cross-backend contract tests. |
| **0.6.0 — MCP / GraphQL adapters** | `zed_mcp_tool` decorator; resolver decorator; FastMCP & strawberry support. |
| **1.0.0 — Stable release** | Full docs, CI matrix green, audit-log model, `select_related` compiler hook (or carved to 1.1). |
| **1.x** | `select_related` SQL compiler; bulk operations; `Meta.protected_fields` (descriptor-based field gating for regulated tenants); PostgreSQL RLS defense-in-depth track. |

---

## Open questions

1. **Relationship table partitioning at scale.** Above ~100M rows, PostgreSQL recursive CTEs slow even with the indexes shipped. Worth designing a `(resource_type)` LIST partition scheme? **Lean: yes, post-1.0**, document the threshold and shipped migration helper.

2. **Swappable User dependency.** `auth/user` is hardcoded as a subject type label. Projects with `AUTH_USER_MODEL` aliases (`accounts.User`) need... what? Lean: a `ZEDRBAC_USER_TYPE` setting (default `"auth/user"`), plus `to_subject_ref()` consults `settings.AUTH_USER_MODEL` to decide. Settle in 0.1.

3. **Async ORM support.** Django 5.0+ has `aget` / `asave`. Should `ZedRBACManager` ship async variants? Lean: yes, but in 0.5 — first release sync only. The contextvar propagation works correctly with `asgiref.sync.sync_to_async` already.

4. **Override layer precedence.** When `PermissionOverride` rows tighten a permission AND a caveat returns `CONDITIONAL`, what wins? Lean: tightening wins (security-fail-closed). Documented as a doctor warning when this collision is detected at compile time.

5. **MCP authentication standardisation.** As of May 2026, `ctx.request_context.meta` is the de facto channel for actor identity. If the MCP spec adds a typed identity field in 2026/2027, the plugin should adopt it without a major version bump. Open issue, watching upstream.

6. **GraphQL field-level redaction.** The Layer-2 redaction pattern from Angee's spec applies cleanly to DRF. GraphQL field resolvers run independently, and field-level gating is naturally per-resolver — does the plugin need a `ZedField` class, or is `@require_permission` on the resolver enough? Lean: latter, ship in 0.6.

7. **Web admin for the override layer.** v1.0 ships a Django admin form. A standalone admin SPA (separate optional package, `django-zedrbac-admin`) could be more usable. Defer — gather user feedback first.

---

## Appendix — what `django-zedrbac` is not

- **Not a User model.** Use `django.contrib.auth.models.User` or any swappable `AUTH_USER_MODEL`.
- **Not an authentication system.** Use `django-allauth`, `dj-rest-auth`, `simple-jwt`, `python-social-auth`, or your own.
- **Not a session manager.** Django's session middleware is fine.
- **Not a multi-tenant database router.** Use `django-tenants` or `django-organizations`. `django-zedrbac` is orthogonal — REBAC works within whatever tenant scope the project provides. (You CAN use `ZEDRBAC_TYPE_PREFIX = "tenant_acme/"` for soft-tenant scoping if rows-per-tenant fit in one DB.)
- **Not an audit-log system.** A future `django-zedrbac-audit` package may add one; v1 emits structured logs only.
- **Not a policy DSL** like Polar or Cedar. The schema language is SpiceDB's `.zed`, which is REBAC-first. ABAC fragments are expressed via caveats.
