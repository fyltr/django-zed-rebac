# Defining Permissions in `django-zed-rebac`

> Last updated: 2026-05-01
> Status: **draft for review** — first public guide.
> Audience: Django developers writing permission schemas. Read [SPEC.md](./SPEC.md) first for the architecture.

---

## What is a "schema" in `django-zed-rebac`?

A **schema** is a typed graph of who-can-do-what, expressed in SpiceDB's `.zed` schema language. It defines:

1. **Resource types** (`blog/post`, `auth/user`, `mcp/tool/edit_document`).
2. **Relations** between them (`relation owner: auth/user`).
3. **Permissions** computed from those relations (`permission read = owner + viewer`).
4. **Caveats** — runtime-context predicates (`caveat ip_in_cidr(ip, cidr) { ip.in_cidr(cidr) }`).

You author it in **Python** using the `schema as s` builder. The plugin compiles it to a deterministic `.zed` file at `python manage.py rebac build` time. Both backends (`LocalBackend`, `SpiceDBBackend`) consume the same compiled output.

This document is the user-facing guide: how to author the schema for the entities Django projects actually have. The implementation details (build pipeline, backends, manager wiring) are in [SPEC.md](./SPEC.md).

---

## The minimum viable schema

Three lines of Python make a model permission-aware:

```python
from django.db import models
from rebac import RebacMixin, schema as s

class Post(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("owner",   to="auth/user"),
            s.permission("read",  expr="owner"),
            s.permission("write", expr="owner"),
        ]
```

After `python manage.py rebac build` you get this fragment in `rebac/schema.zed`:

```zed
definition blog/post {
    relation owner: auth/user

    permission read = owner
    permission write = owner
}
```

`Post.objects.with_actor(request.user).all()` now returns only posts the user is the `owner` of. (`with_actor()` is the generic verb; `Post.objects.as_user(request.user)` is the typed shorthand for the Django-User case — same result.) To grant access, write a relationship:

```python
from rebac import backend, ObjectRef, SubjectRef, RelationshipTuple

backend().write_relationships([
    ("create", RelationshipTuple(
        resource = ObjectRef("blog/post", post.public_id),
        relation = "owner",
        subject  = SubjectRef(ObjectRef("auth/user", str(user.pk))),
    )),
])
```

---

## The schema-builder API

Five primitives, all importable from `rebac.schema`:

| Builder call | What it produces in `.zed` |
|---|---|
| `s.relation("name", to="auth/user")` | `relation name: auth/user` |
| `s.relation("name", to=["auth/user", "auth/group#member"])` | `relation name: auth/user \| auth/group#member` |
| `s.permission("name", expr="...")` | `permission name = ...` |
| `s.caveat("name", parameters=[("k", "string")], expression="k == 'x'")` | `caveat name(k string) { k == 'x' }` |
| `s.definition("type", relations=[...], permissions=[...])` | `definition type { ... }` (top-level, used outside `Meta.permission_relations`) |

`Meta.permission_relations` accepts a flat list of `s.relation()` and `s.permission()` calls. The plugin synthesises the surrounding `definition <app_label>/<model_name> { ... }`.

For schema fragments that aren't tied to a single model — base subject types like `auth/user`, helper resources like `agents/grant` (shipped by your `agents` app, not by this plugin), package-wide caveats — declare them in a `permissions.py` module at app level:

```python
# blog/permissions.py
from rebac import schema as s

# Standalone definitions outside of any specific Meta:
s.definition("blog/category", relations=[
    s.relation("subscriber", to="auth/user"),
    s.permission("read", expr="subscriber"),
])

# Caveats:
s.caveat(
    name        = "ip_in_cidr",
    parameters  = [("ip", "ipaddress"), ("cidr", "string")],
    expression  = "ip.in_cidr(cidr)",
)
```

The `rebac` AppConfig auto-discovers `permissions.py` in every installed app at app-ready (importlib-style — same pattern as `admin.py`, `signals.py`).

---

## Operator precedence — the one footgun you must remember

SpiceDB's expression operators have an order **different from most languages**:

```
+   (union)            ← binds tightest
&   (intersection)
-   (exclusion)        ← binds loosest
```

So `a + b & c` parses as `(a + b) & c`. **Always use explicit parentheses** in compound expressions:

```python
# ❌ Subtle. Means (owner + editor) & published.
s.permission("read", expr="owner + editor & published")

# ✅ Explicit. Reads as you mean it.
s.permission("read", expr="(owner + editor) & published")
```

The build emits `use typechecking` at the top of `schema.zed`, which catches the most common semantic bugs (e.g., intersecting two mutually-exclusive subject types). It does NOT catch precedence mistakes — those are syntactically valid.

---

## Patterns by scenario

### Users and Groups (the `django.contrib.auth` integration)

`django-zed-rebac` does not ship a `User` model. It declares two base types — `auth/user` and `auth/group` — and assumes Django's `User` and `Group` provide the actual rows.

#### The base types (auto-emitted)

```zed
// emitted by rebac itself; you don't write this
definition auth/user {}

definition auth/group {
    relation member: auth/user | auth/group#member
}
```

`auth/group#member` is a SpiceDB **subject set** — a way to grant a relation to "everyone in this group" without enumerating each user.

#### Granting a relation to a Group

```python
class Post(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("owner",  to="auth/user"),
            s.relation("viewer", to=["auth/user", "auth/group#member"]),

            s.permission("read",  expr="owner + viewer"),
            s.permission("write", expr="owner"),
        ]
```

To grant `viewer` to every member of group `editors`:

```python
backend().write_relationships([
    ("create", RelationshipTuple(
        resource = ObjectRef("blog/post", post.public_id),
        relation = "viewer",
        subject  = SubjectRef(
            object             = ObjectRef("auth/group", str(editors_group.pk)),
            optional_relation  = "member",
        ),
    )),
])
```

Anyone in the `editors` group now has `read` permission on the post.

#### Syncing Django's `User.groups` M2M into REBAC

The plugin ships an opt-in signal handler that mirrors `User.groups` changes into `Relationship` rows of the form `auth/group:<id>#member @ auth/user:<id>`. Enable via:

```python
REBAC_SYNC_DJANGO_GROUPS = True
```

This is a one-way sync (Django → REBAC). If you want bidirectional sync, you're in custom-territory.

#### Public read access (`user:*`)

Use the `user:*` wildcard sparingly:

```python
s.relation("public_viewer", to=["auth/user", "auth/user:*"]),
s.permission("read",        expr="owner + public_viewer"),
```

To make a post world-readable:

```python
backend().write_relationships([
    ("create", RelationshipTuple(
        resource = ObjectRef("blog/post", post.public_id),
        relation = "public_viewer",
        subject  = SubjectRef(ObjectRef("auth/user", "*")),
    )),
])
```

**Wildcard rules:**

- **Only ever use wildcards on read-shaped permissions.** A `public_editor` wildcard means anyone can edit anything. The schema builder warns when a wildcard appears in a write/delete permission.
- **Wildcards cannot be transitively included.** `relation viewer: auth/user | auth/group#member` cannot have `auth/user:*` smuggled in via the group; only direct `auth/user` types support wildcards.
- **Wildcards make `LookupSubjects` expensive** (the result is "all users"). They are cheap for `CheckPermission`.

### Hierarchical resources (folders → files)

The classic recursive permission. A user has `read` on a file if they have `read` on its parent folder.

```python
class Folder(RebacMixin, models.Model):
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE)

    class Meta:
        permission_relations = [
            s.relation("owner",  to="auth/user"),
            s.relation("viewer", to=["auth/user", "auth/group#member"]),
            s.relation("parent", to="storage/folder"),

            s.permission("read",  expr="owner + viewer + parent->read"),
            s.permission("write", expr="owner + parent->write"),
        ]


class File(RebacMixin, models.Model):
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE)

    class Meta:
        permission_relations = [
            s.relation("owner",  to="auth/user"),
            s.relation("folder", to="storage/folder"),

            s.permission("read",  expr="owner + folder->read"),
            s.permission("write", expr="owner + folder->write"),
            s.permission("delete", expr="owner + folder->write"),
        ]
```

The `parent->read` arrow operator says "walk to the parent, then check `read` over there". Recursion is bounded by `REBAC_DEPTH_LIMIT` (default 8). For folder trees deeper than 8, callers receive `PermissionDepthExceeded` — at that point, switch to `SpiceDBBackend` (which also caps depth, but at 50 by default and with substantially better performance for deep walks).

**Multi-hop arrows are NOT supported.** You cannot write `parent->parent->read`. The above pattern works because `read` itself recurses through `parent->read` — that's how multi-hop traversal is expressed in SpiceDB.

**Cycles in the data.** SpiceDB does not prevent you from writing `folder:A#parent @ folder:A`. The dispatcher hits the depth limit and returns `false` (no error). Prevent cycles in your application — a pre-save check that the proposed parent isn't a descendant of the proposed child.

### Time-bound access (use `expiration`, not caveats)

Modern SpiceDB schemas (v1.40+) support relationship expiration as a first-class feature. **Prefer this over the older "current_time as a caveat" pattern** — expiration garbage-collects automatically; caveats don't.

```python
# In permissions.py:
s.directive("use expiration")  # added once at the top of the schema

class Post(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("owner",            to="auth/user"),
            s.relation("temporary_viewer", to="auth/user", with_expiration=True),

            s.permission("read", expr="owner + temporary_viewer"),
        ]
```

Granting a 24-hour view:

```python
from datetime import datetime, timedelta, timezone

backend().write_relationships([
    ("create", RelationshipTuple(
        resource    = ObjectRef("blog/post", post.public_id),
        relation    = "temporary_viewer",
        subject     = SubjectRef(ObjectRef("auth/user", str(user.pk))),
        expires_at  = datetime.now(timezone.utc) + timedelta(hours=24),
    )),
])
```

After 24 hours, `LocalBackend` excludes the row from query results; the GC task (`rebac.gc.expire_relationships`) deletes it permanently within `REBAC_GC_INTERVAL_SECONDS` (default 5 minutes).

### Conditional access (caveats — runtime context)

Caveats are CEL expressions evaluated against runtime context at check time. Use them for **dynamic** constraints — IP allow-lists, business hours, on-call rotation membership. For **static** time bounds, prefer `use expiration`.

```python
# In permissions.py:
s.caveat(
    name        = "ip_in_cidr",
    parameters  = [("ip", "ipaddress"), ("cidr", "string")],
    expression  = "ip.in_cidr(cidr)",
)

s.caveat(
    name        = "during_business_hours",
    parameters  = [("hour", "int"), ("start", "int"), ("end", "int")],
    expression  = "hour >= start && hour <= end",
)
```

In a model:

```python
class SensitiveDoc(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("owner",  to="auth/user"),
            s.relation("ip_restricted_viewer", to="auth/user", with_caveat="ip_in_cidr"),

            s.permission("read", expr="owner + ip_restricted_viewer"),
        ]
```

When writing the relationship, supply the static parameters (caveat *context*):

```python
backend().write_relationships([
    ("create", RelationshipTuple(
        resource       = ObjectRef("docs/sensitive", doc.public_id),
        relation       = "ip_restricted_viewer",
        subject        = SubjectRef(ObjectRef("auth/user", str(user.pk))),
        caveat_name    = "ip_in_cidr",
        caveat_context = {"cidr": "10.0.0.0/8"},
    )),
])
```

When checking, supply the runtime parameter:

```python
result = backend().check_permission(
    subject    = to_subject_ref(user),
    permission = "read",
    resource   = ObjectRef("docs/sensitive", doc.public_id),
    context    = {"ip": request.META["REMOTE_ADDR"]},
)
# result.allowed: True if user.ip is in 10.0.0.0/8
# result.conditional_on: [] if all caveat context was supplied
```

If you check WITHOUT supplying `ip` in the context, `result` returns `CONDITIONAL_PERMISSION` with `conditional_on=["ip"]`. The application can then re-check with the missing field — useful for two-pass evaluation (cheap relationship check + expensive context resolution).

**`LocalBackend` caveat support.** Backed by [`cel-python`](https://pypi.org/project/cel-python/). Most CEL types work out of the box (`int`, `string`, `bool`, `list`, `map`, `timestamp`, `duration`). The `ipaddress` type is **not** supported by `cel-python` natively — `LocalBackend` raises `CaveatUnsupportedError` for caveats using it. Migrate to `SpiceDBBackend` if `ipaddress` matters; or rewrite the caveat to pass IPs as strings and do the CIDR check in Python via a custom evaluator.

### MCP tools as resources

MCP (Model Context Protocol) tools are first-class resources. Permissions on them gate which tools an actor can invoke.

#### Pattern 1 — one resource type per tool

```python
# mcp_tools/permissions.py
s.definition("mcp/tool/query_posts", relations=[
    s.relation("invoker", to=["auth/user", "agents/agent#operator"]),
    s.permission("invoke", expr="invoker"),
])

s.definition("mcp/tool/edit_post", relations=[
    s.relation("invoker", to="auth/user"),     # narrower — no agent invocation
    s.permission("invoke", expr="invoker"),
])
```

Wire the tool with the plugin's MCP decorator:

```python
from rebac.mcp import rebac_mcp_tool

@mcp.tool
@rebac_mcp_tool(resource_type="mcp/tool/query_posts", action="invoke")
async def query_posts(query: str, ctx: Context = CurrentContext()) -> list[dict]:
    ...
```

Granting the right to invoke:

```python
backend().write_relationships([
    ("create", RelationshipTuple(
        resource = ObjectRef("mcp/tool/query_posts", "*"),  # "*" = the tool itself, no instance
        relation = "invoker",
        subject  = SubjectRef(ObjectRef("auth/user", str(user.pk))),
    )),
])
```

#### Pattern 2 — group tools into capability categories

For larger projects, one resource type per tool gets unwieldy. Group them:

```python
s.definition("mcp/capability", relations=[
    s.relation("granted_to", to=["auth/user", "agents/agent#operator"]),
    s.permission("use", expr="granted_to"),
])
```

Then tag tools with the capability they require:

```python
@mcp.tool
@rebac_mcp_tool(resource_type="mcp/capability", action="use", id_arg="_capability")
async def query_posts(query: str, ctx: Context = CurrentContext(), *, _capability: str = "blog.read") -> list[dict]:
    ...
```

The `_capability="blog.read"` argument is filtered out of the published MCP schema (clients don't see it) but tells the decorator which capability to check. Capabilities form a flat namespace (`blog.read`, `blog.write`, `admin.users`, etc.) — easy for admins to grant in bulk.

### Agents acting on behalf of users (the Grant pattern)

The canonical Authzed-recommended pattern for AI agents. An agent's effective permission on any resource is automatically the **structural intersection** of (a) the user's grants on that resource and (b) the agent's declared capabilities — enforced by the schema graph, not by app-layer ANDs.

#### The agents/agent and agents/grant definitions (NOT auto-emitted)

These types are NOT shipped by `django-zed-rebac`. They live in a separate `agents` app in your project (or in a downstream framework that supplies one). The plugin only auto-emits `auth/user` and `auth/group`, which map onto `django.contrib.auth`. Everything in the `agents/` namespace is your code.

A typical `agents/permissions.py` ships:

```python
# agents/permissions.py — in YOUR agents app, not in rebac
from rebac import schema as s

s.definition("agents/agent", relations=[
    s.relation("operator",       to="auth/user"),
    s.relation("has_capability", to="agents/capability"),
])

s.definition("agents/capability")    # marker type; relation rows tie capabilities to agents

s.definition("agents/grant", relations=[
    s.relation("user",  to="auth/user"),
    s.relation("agent", to="agents/agent"),

    s.permission("active", expr="user & agent"),
])
```

A `Grant` row encodes "user U has delegated to agent A". Both relations must be present for `active` to resolve true.

#### Targeting resources from grants

Add `agents/grant#active` to the type union of any relation that should accept agent invocation:

```python
class Post(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("owner",  to="auth/user"),
            s.relation("viewer", to=[
                "auth/user",
                "auth/group#member",
                "agents/grant#active",      # ← agents acting on behalf of users
            ]),

            s.permission("read",  expr="owner + viewer"),
            s.permission("write", expr="owner"),
        ]
```

The clever bit: when `subject = agents/grant:G123`, SpiceDB's evaluator walks the grant's `user` relation, recursively asks "is this user a viewer/owner?", and only returns `true` if so. The agent **inherits** the user's view but can never exceed it.

#### Bounding the agent further by capability

To restrict a specific agent to a subset of actions (e.g., agent X can `read` posts but not `write` even when the user can):

```python
# In permissions.py:
s.definition("blog/post_capability", relations=[
    s.relation("read_capability",  to="auth/capability"),
    s.relation("write_capability", to="auth/capability"),
])

class Post(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("owner",  to="auth/user"),
            s.relation("viewer", to=["auth/user", "agents/grant#active"]),

            s.permission("read",
                expr="owner + viewer & blog/post_capability:_self->read_capability"),
            s.permission("write",
                expr="owner & blog/post_capability:_self->write_capability"),
        ]
```

The `agent->has_capability` walk inside the permission expression intersects the user's grant with the agent's declared capabilities.

#### Per-grant exceptions via caveats

Sometimes you want a grant valid for a specific time window, IP range, or model kind:

```python
s.caveat(
    name        = "grant_constraints",
    parameters  = [
        ("now",         "timestamp"),
        ("expires_at",  "timestamp"),
        ("model_kind",  "string"),
        ("allowed_kinds", "list<string>"),
    ],
    expression  = "now < expires_at && model_kind in allowed_kinds",
)

# In agents/grant: attach the caveat to specific relationships
backend().write_relationships([
    ("create", RelationshipTuple(
        resource    = ObjectRef("agents/grant", grant.public_id),
        relation    = "user",
        subject     = SubjectRef(ObjectRef("auth/user", str(user.pk))),
        caveat_name = "grant_constraints",
        caveat_context = {
            "expires_at":    "2026-12-31T23:59:59Z",
            "allowed_kinds": ["claude_internal", "claude_external"],
        },
    )),
])
```

When the agent invokes a tool, the request passes `now` and `model_kind` in caveat context — the grant only resolves `active` if both context-side checks pass.

#### Querying as an agent — `with_actor` is the generic verb

The queryset-scoping verb is `with_actor(actor)`, where `actor` is any subject — Django `User`, registered `Agent`, `agents/grant` reference, `auth/apikey`, etc. Two typed shorthands handle the common cases:

```python
# Common case: HTTP request from a Django user
Post.objects.as_user(request.user)
# expands to: Post.objects.with_actor(to_subject_ref(request.user))
#                       → SubjectRef(auth/user:<id>)

# Agent acting on behalf of a user (the canonical Grant pattern)
Post.objects.as_agent(agent, on_behalf_of=request.user)
# expands to: Post.objects.with_actor(grant_subject_ref(agent, request.user))
#                       → SubjectRef(agents/grant:<grant_id>#valid)

# Or pass any SubjectRef directly:
Post.objects.with_actor(SubjectRef(ObjectRef("agents/grant", grant.public_id)))
Post.objects.with_actor(SubjectRef(ObjectRef("auth/apikey", apikey.public_id)))

# Anything @rebac_subject-decorated also resolves automatically:
Post.objects.with_actor(my_apikey_instance)
```

Inside an MCP tool (where the canonical actor is an `agents/grant`), use `as_agent`:

```python
@mcp.tool
@rebac_mcp_tool(resource_type="blog/post", action="write", id_arg="post_id")
async def edit_post(post_id: str, body: str, ctx: Context = CurrentContext()) -> dict:
    user, agent = ctx.rebac.user, ctx.rebac.agent     # populated by rebac_mcp_tool
    post = await Post.objects.as_agent(agent, on_behalf_of=user).aget(public_id=post_id)
    post.body = body
    await post.asave()                            # re-checks `write` against the same agents/grant
    return {"ok": True}
```

The grant subject's permission walk is the structural intersection of (a) the user's grants on `blog/post` and (b) the agent's declared capabilities — enforced by the schema graph (see [§ Targeting resources from grants](#targeting-resources-from-grants) above), not by app-layer ANDs. The agent inherits the user's view; can never exceed it.

`with_actor` does NOT mutate `current_actor()` — the originating actor (typically the request's Django user) is preserved for audit. This is the Odoo `with_user` / `env.user` independence, made explicit. See [SPEC.md § `with_actor` vs `sudo`](./SPEC.md#with_actor-vs-sudo--distinct-verbs).

### Celery tasks acting on behalf of users

Tasks inherit the actor automatically when wired through the plugin's signals (see [SPEC.md § Celery](./SPEC.md#celery)). The schema authoring is the same as for HTTP requests — you don't declare separate "task" resource types unless tasks themselves are gated.

If they are (e.g., "only ops users can run the reindex task"), declare them as resources:

```python
s.definition("celery/task/reindex_posts", relations=[
    s.relation("runner", to="auth/user"),
    s.permission("invoke", expr="runner"),
])

# In the task:
from rebac import backend, ObjectRef, current_actor

@shared_task
def reindex_posts():
    actor = current_actor()
    if actor is None:
        raise PermissionDenied("no actor on this task invocation")
    result = backend().check_permission(
        subject    = actor,
        permission = "invoke",
        resource   = ObjectRef("celery/task/reindex_posts", "*"),
    )
    if not result.allowed:
        raise PermissionDenied(...)
    # ... do work ...
```

Or with the `@require_permission` decorator:

```python
from rebac import require_permission

@shared_task
@require_permission(
    action        = "invoke",
    resource_type = "celery/task/reindex_posts",
    resource_id   = "*",
)
def reindex_posts():
    ...
```

### DRF viewsets (permissions are inherited from the model)

DRF integration requires NO additional schema authoring. The model's `permission_relations` are the source of truth; `RebacPermission` and `RebacFilterBackend` consult them.

```python
class PostViewSet(viewsets.ModelViewSet):
    queryset           = Post.objects.all()
    serializer_class   = PostSerializer
    permission_classes = [RebacPermission]
    filter_backends    = [RebacFilterBackend]
```

`list` → checks `read` per row. `retrieve` → `read`. `create` → `create` (model-level). `update`/`partial_update` → `write`. `destroy` → `delete`. Customise via `view.action_map` if your schema uses different action names:

```python
class PostViewSet(viewsets.ModelViewSet):
    permission_classes = [type("MyPerm", (RebacPermission,), {
        "action_map": {**RebacPermission.action_map, "publish": "publish"},
    })]
```

For non-CRUD viewset actions:

```python
class PostViewSet(viewsets.ModelViewSet):
    @action(detail=True, methods=["post"])
    @require_permission("publish")
    def publish(self, request, pk):
        post = self.get_object()
        post.published = True
        post.save()
        return Response(...)
```

### Plain Python entities

Anything with a stable string ID can be a resource. Use the `@rebac_resource` decorator to register the type:

```python
from rebac import rebac_resource, schema as s

s.definition("storage/s3_prefix", relations=[
    s.relation("reader", to="auth/user"),
    s.permission("read",  expr="reader"),
])

@rebac_resource(type="storage/s3_prefix", id_attr="prefix")
class S3Prefix:
    def __init__(self, prefix: str):
        self.prefix = prefix
```

Now any code can check:

```python
from rebac import backend, to_object_ref, to_subject_ref

prefix = S3Prefix("uploads/2026/")
result = backend().check_permission(
    subject    = to_subject_ref(user),
    permission = "read",
    resource   = to_object_ref(prefix),     # uses id_attr=prefix
)
```

This makes REBAC available for any resource boundary your project has, not just Django ORM rows.

### Multi-tenant scoping (soft tenants)

For projects where one Django DB serves multiple tenants, use `REBAC_TYPE_PREFIX`:

```python
# settings.py — set per-tenant before request handling
REBAC_TYPE_PREFIX = "tenant_acme/"
```

Every resource type emitted by the schema becomes `tenant_acme/blog/post`. Relationships from one tenant cannot be referenced by another.

For hard-tenant isolation (separate databases, schemas), use `django-tenants` and let each tenant have its own `Relationship` table — no schema changes needed.

---

## Composing schemas across packages

`django-zed-rebac`'s build walks every installed app and collects:

1. Every `Meta.permission_relations` from `RebacMixin` models.
2. Every top-level `s.definition(...)` / `s.caveat(...)` in any app's `permissions.py`.
3. Every `@rebac_resource(...)` registration.

It composes them by **alphabetical order of resource type** (deterministic), validates references (every type mentioned must be defined), and emits one unified `schema.zed`.

**Cross-app references are first-class:**

```python
# In blog/models.py — references storage/folder declared in storage/permissions.py
class Post(RebacMixin, models.Model):
    folder = models.ForeignKey("storage.Folder", on_delete=models.CASCADE)

    class Meta:
        permission_relations = [
            s.relation("folder", to="storage/folder"),       # cross-app reference
            s.permission("read", expr="owner + folder->read"),
        ]
```

The build raises a clear error if `storage/folder` isn't defined anywhere. **Cycles between apps on schema (drive → auth → drive) are rejected** at build time — the dependency DAG enforces ordering.

---

## Anti-patterns to avoid

### 1. Don't put `auth/user:*` (wildcards) in write-shaped permissions

```python
# ❌ Anyone can edit anything.
s.relation("editor", to=["auth/user", "auth/user:*"]),
s.permission("write", expr="editor"),
```

The build emits a warning when a wildcard relation participates in a `write`/`delete`/`create` permission. Wildcards are for read-shaped, public-share patterns only.

### 2. Don't smuggle wildcards transitively

```python
# ❌ SpiceDB rejects this at WriteSchema time.
s.relation("viewer", to=["auth/user:*", "auth/group#member"]),
# Wildcards cannot flow through subject sets like group#member.
```

The error message: "wildcard relations cannot be transitively included." The plugin's system check catches this before runtime.

### 3. Don't write circular `parent` relations in your data

```python
# Schema is fine; data corrupts the graph:
folder:A#parent @ folder:B
folder:B#parent @ folder:A   # ← cycle
```

SpiceDB's dispatcher hits the depth limit and returns `false` — no error, just a quiet "no permission". Validate at the application layer:

```python
def assign_parent(self, new_parent):
    if self.is_descendant_of(new_parent):
        raise ValidationError("would create cycle")
    self.parent = new_parent
```

### 4. Don't intersect mutually-exclusive subject types

```python
# ❌ user is type auth/user; admin is type billing/admin (a hypothetical
# unrelated subject type). This permission is never satisfiable.
s.permission("edit", expr="user & admin"),
```

The build emits `use typechecking` automatically; SpiceDB then catches this at WriteSchema time. But a friendly error from the build itself is faster — the plugin runs the same typecheck locally and refuses to emit the schema.

### 5. Don't forget operator parens in compound expressions

```python
# ❌ (a + b) & c, probably not what you meant.
s.permission("read", expr="a + b & c"),

# ✅
s.permission("read", expr="a + (b & c)"),
```

Documented up-front in [§ Operator precedence](#operator-precedence--the-one-footgun-you-must-remember) above.

### 6. Don't author large schemas in `Meta.permission_relations`

For models with 20+ permissions, prefer the `permissions.py` module. Keep `Meta` focused on the data model:

```python
# blog/permissions.py — the schema lives here
post_perms = [
    s.relation("owner",  to="auth/user"),
    s.relation("editor", to="auth/user"),
    # ... 20 more ...
]

# blog/models.py — clean
from .permissions import post_perms

class Post(RebacMixin, models.Model):
    class Meta:
        permission_relations = post_perms
```

### 7. Don't model agents as principals — use the Grant pattern

```python
# ❌ Bypasses the user's grants entirely.
s.relation("agent_viewer", to="agents/agent"),
s.permission("read", expr="owner + agent_viewer"),
```

The Grant pattern (`agents/grant#active`) is canonical because the agent's permission flows through the user's grants — they can never exceed the user. Modeling agents as direct principals is a security smell.

---

## Patterns library — copyable starting points

Each section below is a complete, working schema fragment. Copy and adapt.

### Pattern A — RBAC (role-based access control) on a model

```python
class Document(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("admin",  to="auth/user"),
            s.relation("writer", to="auth/user"),
            s.relation("reader", to=["auth/user", "auth/group#member"]),

            s.permission("read",   expr="admin + writer + reader"),
            s.permission("write",  expr="admin + writer"),
            s.permission("delete", expr="admin"),
        ]
```

### Pattern B — Hierarchical resources (folders → docs)

```python
class Folder(RebacMixin, models.Model):
    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE)
    class Meta:
        permission_relations = [
            s.relation("owner",  to="auth/user"),
            s.relation("parent", to="docs/folder"),
            s.permission("read",  expr="owner + parent->read"),
            s.permission("write", expr="owner + parent->write"),
        ]


class Document(RebacMixin, models.Model):
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE)
    class Meta:
        permission_relations = [
            s.relation("owner",  to="auth/user"),
            s.relation("folder", to="docs/folder"),
            s.permission("read",  expr="owner + folder->read"),
            s.permission("write", expr="owner + folder->write"),
        ]
```

### Pattern C — Ownership + group sharing

```python
class Post(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("owner",          to="auth/user"),
            s.relation("group_member",   to="auth/group#member"),

            s.permission("read",  expr="owner + group_member"),
            s.permission("write", expr="owner"),
        ]
```

### Pattern D — Public-readable, group-writable

```python
class Page(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("editor",        to="auth/user"),
            s.relation("public_viewer", to=["auth/user", "auth/user:*"]),

            s.permission("read",  expr="editor + public_viewer"),
            s.permission("write", expr="editor"),
        ]
```

### Pattern E — Time-bound shared access (expiration)

```python
# permissions.py
s.directive("use expiration")

class Doc(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("owner",            to="auth/user"),
            s.relation("temporary_viewer", to="auth/user", with_expiration=True),

            s.permission("read", expr="owner + temporary_viewer"),
        ]
```

### Pattern F — Conditional access (caveat)

```python
# permissions.py
s.caveat(
    name        = "tenant_match",
    parameters  = [("user_tenant", "string"), ("doc_tenant", "string")],
    expression  = "user_tenant == doc_tenant",
)

class Doc(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("viewer", to="auth/user", with_caveat="tenant_match"),
            s.permission("read", expr="viewer"),
        ]
```

### Pattern G — Agent acting on behalf of user (Grant pattern)

```python
class Doc(RebacMixin, models.Model):
    class Meta:
        permission_relations = [
            s.relation("owner",  to="auth/user"),
            s.relation("viewer", to=[
                "auth/user",
                "auth/group#member",
                "agents/grant#active",   # agent inherits user's view
            ]),

            s.permission("read",  expr="owner + viewer"),
            s.permission("write", expr="owner"),    # agents can read but not write
        ]
```

### Pattern H — MCP tool gated by capability

```python
# mcp/permissions.py
s.definition("mcp/capability", relations=[
    s.relation("granted_to", to=["auth/user", "agents/agent#operator"]),
    s.permission("use",      expr="granted_to"),
])

# mcp/tools.py
@mcp.tool
@rebac_mcp_tool(resource_type="mcp/capability", action="use", id_arg="_capability")
async def search_documents(q: str, ctx: Context = CurrentContext(), *, _capability: str = "docs.search"):
    ...
```

### Pattern I — Celery task gated by role

```python
# permissions.py
s.definition("celery/task/reindex", relations=[
    s.relation("runner", to="auth/user"),
    s.permission("invoke", expr="runner"),
])

# tasks.py
@shared_task
@require_permission(
    action        = "invoke",
    resource_type = "celery/task/reindex",
    resource_id   = "*",
)
def reindex():
    ...
```

### Pattern J — Public Python entity (S3 prefix)

```python
# permissions.py
s.definition("storage/s3_prefix", relations=[
    s.relation("reader", to=["auth/user", "auth/user:*"]),
    s.permission("read",  expr="reader"),
])

# storage.py
@rebac_resource(type="storage/s3_prefix", id_attr="prefix")
class S3Prefix:
    def __init__(self, prefix: str):
        self.prefix = prefix
```

---

## Reference — the schema-language complete grammar

For the full SpiceDB schema language (composable schemas, all directives, every operator), see the [authzed documentation](https://authzed.com/docs/spicedb/concepts/schema). `django-zed-rebac`'s Python builder covers the SpiceDB-canonical subset relevant to Django projects:

- Definitions (top-level, model-level, mcp-tool-level, celery-level)
- Relations with type unions and subject sets
- Permissions with `+`, `&`, `-`, arrows (`->`)
- Caveats (parameters, CEL expressions)
- Wildcards (`type:*`)
- Self-referential relations (parent chains)
- `use expiration` directive
- `use typechecking` directive (auto-emitted)

NOT covered by the v1 builder (use raw `.zed` import for these):
- Composable schemas (`use import`, partial fragments). Defer to multi-package composition via `permissions.py` modules instead.
- `use self` (subject-equals-resource shortcut). Niche; raise an issue if needed.
- `nil` type. Almost never useful in practice.

---

## Where to look next

- [SPEC.md](./SPEC.md) — full implementation specification: architecture, public API, settings, surfaces, determinism, testing, roadmap.
- [SpiceDB schema docs](https://authzed.com/docs/spicedb/concepts/schema) — upstream schema-language reference.
- [Authzed "Secure AI Agents" tutorial](https://authzed.com/docs/spicedb/tutorials/ai-agent-authorization) — the canonical Grant-pattern walkthrough.
- [Zanzibar paper](https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/) — the conceptual origin.
