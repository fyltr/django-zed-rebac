# django-zedrbac

> SpiceDB-compatible REBAC for any Django project. Drop in, declare your permission schema in Python, and every queryset, save, and method call is gated against the effective user.

---

> **Status: pre-alpha.** The spec is settled and reviewed. Implementation is in progress; nothing is on PyPI yet. Track the milestones at [docs/SPEC.md § Roadmap](./docs/SPEC.md#roadmap).

---

## What it is

`django-zedrbac` ports [SpiceDB](https://github.com/authzed/spicedb)'s relation-based access control model into Django. You author your permission schema in Python next to your models; the plugin compiles it to a byte-deterministic `.zed` file plus a runtime manifest. Two backends are interchangeable behind one Python API:

- **`LocalBackend`** — pure Django, evaluates permissions via recursive CTEs against a single `Relationship` table. Zero external infrastructure. Suitable up to ~10M relationships and depth ≤ 8.
- **`SpiceDBBackend`** — wraps the official [`authzed`](https://pypi.org/project/authzed/) Python client. Connects to a SpiceDB cluster. Drop-in swap when `LocalBackend` is no longer enough — same Python API, no code changes, just `ZEDRBAC_BACKEND = "spicedb"` in settings.

Add the mixin to your model and `Post.objects.all()` returns only what the user can read. Add `Model.objects.as_user(some_user)` for explicit actor scoping in MCP servers, Celery tasks, GraphQL resolvers, and management commands.

## Quickstart

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

ZEDRBAC_BACKEND = "local"
```

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
            s.relation("owner",   to="auth/user"),
            s.relation("viewer",  to=["auth/user", "auth/group#member"]),
            s.permission("read",  expr="owner + viewer"),
            s.permission("write", expr="owner"),
        ]
```

```bash
python manage.py migrate          # creates the Relationship table
python manage.py zedrbac build    # emits zedrbac/schema.zed + permissions.json
```

```python
# blog/views.py
def post_detail(request, pk):
    post = get_object_or_404(Post.objects.as_user(request.user), pk=pk)
    return render(request, "post.html", {"post": post})
```

That's the end-to-end flow. The same `Post.objects.as_user(...)` pattern works in DRF viewsets, Celery tasks, MCP tools, and GraphQL resolvers.

## Documentation

| Doc | When to read it |
|---|---|
| **[docs/SPEC.md](./docs/SPEC.md)** | You're integrating, contributing, or evaluating fit. Architecture, public API, settings, DRF/Celery/MCP/GraphQL surfaces, determinism, testing, roadmap. |
| **[docs/ZED.md](./docs/ZED.md)** | You're writing permission schemas. How to define permissions for users, groups, MCP tools, AI agents (Grant pattern), Celery tasks, hierarchical resources, time-bound access, arbitrary Python entities. Includes a patterns library and anti-patterns to avoid. |

## Why use this

| Problem | Existing options | What `django-zedrbac` does |
|---|---|---|
| Per-object permissions in Django | `django-guardian` (per-object ACL via GenericFK; no JOIN propagation; no graph traversal) | True REBAC graph; SpiceDB-compatible; manager-level queryset scoping; cross-relation propagation. |
| Run SpiceDB-style permissions locally without infrastructure | None — SpiceDB itself is a Go binary that needs Postgres + a sidecar | `LocalBackend`: recursive CTE on a single Django table. Same API surface as `SpiceDBBackend`. |
| AI-agent authorization | Cedar (no graph traversal); Casbin (in-memory post-filter); Polar/Oso (deprecated 2023) | Native Authzed Grant pattern: an agent acting on behalf of a user receives the *structural intersection* of the user's grants and the agent's declared capabilities — enforced by the schema graph, not app-layer ANDs. |
| Permission scoping outside HTTP | Manual `if user.has_perm(...)` everywhere | `Model.objects.as_user(user)` and `instance.as_user(user)` work in MCP servers, Celery tasks, cron, management commands, plain Python — anywhere. |
| Strict-by-default (no silent leaks) | `django-guardian` returns all rows when nothing scopes; easy to forget | Querysets without an actor raise `MissingActorError` rather than returning everything. Bypass requires explicit `.sudo(reason=...)` and is logged. |

## Highlights

- **One mixin gates everything.** Add `ZedRBACMixin` to a model, and queries / writes / method calls / FK reverse accessors are all permission-aware. No per-viewset wiring.
- **Strict by default.** A queryset without an actor scope raises rather than leaking. Bypass requires explicit `.sudo(reason="cron.expire_drafts")` and writes a structured audit event.
- **Drop-in DRF integration.** `permission_classes = [ZedPermission]` + `filter_backends = [ZedFilterBackend]`. Per-action permission map (list/retrieve→`read`, create→`create`, update→`write`, destroy→`delete`); customisable.
- **Celery actor propagation built in.** `before_task_publish` injects the actor into task headers; `task_prerun` restores it on the worker. Inside `@shared_task`, scoping happens transparently.
- **MCP-aware.** `@zed_mcp_tool` decorator wraps FastMCP / official-SDK tool functions; resolves the actor from `ctx.request_context.meta`; checks before the tool body runs.
- **Three-state checks.** Like SpiceDB, `check_permission()` returns `HAS_PERMISSION`, `NO_PERMISSION`, or `CONDITIONAL_PERMISSION(missing=[...])` — the latter lists which caveat fields the caller must supply for a definitive answer.
- **Type-checked.** Ships `py.typed`. `ZedRBACManager[M]` is `Generic[M]` — your IDE infers the right model class through the manager. `mypy --strict` and `pyright` both run on CI.
- **Deterministic build.** `python manage.py zedrbac build --check` is a CI gate that returns non-zero on schema drift, mirroring `migrate --check`.

## Compatibility

| Python | Django | Status |
|---|---|---|
| 3.11 / 3.12 / 3.13 / 3.14 | 4.2 LTS | ✅ planned |
| 3.11 / 3.12 / 3.13 / 3.14 | 5.2 LTS | ✅ planned |
| 3.13 / 3.14 | 6.0 | ✅ planned |

Versioning follows [DjangoVer](https://www.b-list.org/weblog/2024/nov/18/djangover/): `<DJANGO_MAJOR>.<DJANGO_FEATURE>.<PACKAGE_VERSION>`. Example: `6.0.1` means "works with Django 6.0, package iteration 1".

Database support: PostgreSQL 13+ (production target), MySQL 8+ (supported), SQLite (test/dev only — recursive CTE performance is not production-grade). The `Relationship` table ships with all required indexes in `0001_initial.py`.

## Comparison

| Library | Per-object | Graph traversal | SpiceDB-compatible | AI-agent pattern | Maintained |
|---|:-:|:-:|:-:|:-:|:-:|
| `django-zedrbac` | ✅ | ✅ | ✅ | ✅ Grant pattern | (in development) |
| `django-guardian` | ✅ (ACL via GenericFK) | ❌ | ❌ | ❌ | ✅ |
| `django-rules` | ❌ (predicate engine) | ❌ | ❌ | ❌ | ✅ |
| `django-spicedb` | proxy only | ✅ (via SpiceDB) | ✅ | ❌ | ❌ (early/inactive) |
| `casbin-django-orm-adapter` | ✅ (ACL, in-memory) | partial | ❌ | ❌ | ✅ |
| `django-oso` | ✅ | ✅ | ❌ | ❌ | ❌ (deprecated 2023) |
| `django-rls` (PostgreSQL RLS) | ✅ | DB-level only | ❌ | ❌ | ✅ |
| `zanzipy` | ✅ | ✅ | partial | ❌ | ❌ (early/single-author) |

`django-zedrbac` is the first Django package targeting full SpiceDB schema-language compatibility AND a working in-process backend AND a first-class AI-agent pattern — none of the others combine all three.

## Backends in detail

```
┌─ Your application ──────────────────────────────────────────────┐
│   ZedRBACMixin / ZedPermission / @zed_resource / @zed_mcp_tool   │
│                            │                                      │
│            ┌───────────────▼──────────────────┐                  │
│            │  zedrbac.backends.Backend (ABC)   │                  │
│            └───────────────┬──────────────────┘                  │
│              ┌─────────────┴────────────┐                        │
│              │                          │                        │
│   ┌──────────▼──────────┐   ┌───────────▼───────────┐           │
│   │  LocalBackend       │   │  SpiceDBBackend        │           │
│   │  recursive CTE +    │   │  authzed.api.v1.Client │           │
│   │  cel-python caveats │   │  → gRPC to spicedb     │           │
│   └─────────────────────┘   └────────────────────────┘           │
└──────────────────────────────────────────────────────────────────┘
```

Both backends are line-for-line API-compatible. The migration path is well-defined: prove your schema in `LocalBackend` first, then flip `ZEDRBAC_BACKEND = "spicedb"` and point at a SpiceDB cluster when scale or graph depth demands it. Persisted consistency tokens (`Zookie`s) are not portable across the swap; this is the only operational consideration documented prominently in [SPEC.md § Migration safety](./docs/SPEC.md#migration-safety).

## What `django-zedrbac` is NOT

- **Not a User model.** Use `django.contrib.auth.models.User` or any swappable `AUTH_USER_MODEL`.
- **Not an authentication system.** Use `django-allauth`, `dj-rest-auth`, `simple-jwt`, or your own.
- **Not a session manager.** Django's session middleware is fine.
- **Not a multi-tenant database router.** Use `django-tenants` or `django-organizations`. `django-zedrbac` is orthogonal — it works inside whatever tenant scope the project provides. (For soft tenancy in a single DB, see `ZEDRBAC_TYPE_PREFIX` in the spec.)
- **Not a policy DSL** like Polar or Cedar. The schema language is SpiceDB's `.zed`, REBAC-first. ABAC fragments are expressed via caveats.

## Status & roadmap

This is a **pre-alpha** package. The architecture is settled (see [docs/SPEC.md](./docs/SPEC.md)) but no PyPI release exists yet. Milestones:

- **0.1** — `LocalBackend` MVP, schema builder, `ZedRBACMixin`, system checks, build/check commands.
- **0.2** — Caveats + expiration support.
- **0.3** — Celery propagation + `ActorMiddleware`.
- **0.4** — Override layer for runtime tweaks.
- **0.5** — `SpiceDBBackend` via `authzed-py`.
- **0.6** — MCP / GraphQL adapters.
- **1.0** — Stable release with full docs and CI matrix green.

Track the full plan in [docs/SPEC.md § Roadmap](./docs/SPEC.md#roadmap).

## Contributing

Once the package lands on PyPI, contribution guidelines will live at `CONTRIBUTING.md`. For now, design feedback on the specs is welcome via GitHub issues — schema-language proposals, missing scenarios, integration-surface concerns, anything in [SPEC.md § Open questions](./docs/SPEC.md#open-questions) you'd push back on.

## License

Apache-2.0 (planned, matches `authzed-py`, `cel-python`, and `spicedb` itself).

## Acknowledgments

`django-zedrbac` is a faithful Django port of the model described in [Google's Zanzibar paper](https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/) and as implemented by [SpiceDB](https://github.com/authzed/spicedb). The schema language and API surface mirror SpiceDB's conventions exactly. The Grant pattern for AI-agent authorization is from [Authzed's Secure AI Agents tutorial](https://authzed.com/docs/spicedb/tutorials/ai-agent-authorization). Caveat evaluation in `LocalBackend` uses [`cel-python`](https://github.com/cloud-custodian/cel-python).
