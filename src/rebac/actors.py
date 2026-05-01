"""Actor resolution: ContextVar, Subject conversion, sudo() / system_context().

The ContextVar `_current_actor` is the *ambient* actor (populated by middleware
and Celery prerun hooks). It is read-only at call sites — `set_current_actor`
should only be invoked at framework boundaries.

Per-queryset actors take strict priority over the ContextVar. See
ARCHITECTURE.md § Three actor-resolution paths.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from importlib import import_module
from typing import Any, Callable, Iterator, Protocol, Union

from django.contrib.auth import get_user_model

from .conf import app_settings
from .errors import (
    NoActorResolvedError,
    SudoNotAllowedError,
    SudoReasonRequiredError,
)
from .types import ObjectRef, SubjectRef


# ---------- ContextVar ----------

_current_actor: ContextVar[SubjectRef | None] = ContextVar("rebac_current_actor", default=None)
_sudo_state: ContextVar[dict[str, Any] | None] = ContextVar("rebac_sudo", default=None)


def current_actor() -> SubjectRef | None:
    """The ambient actor. None when no middleware / task hook has populated it."""
    return _current_actor.get()


def set_current_actor(actor: SubjectRef | None) -> None:
    """Mutate the ambient actor.

    Call only at framework boundaries (middleware, Celery prerun, MCP entry).
    Application code should pass the actor through the queryset, not via this.
    """
    _current_actor.set(actor)


def is_sudo() -> bool:
    return _sudo_state.get() is not None


def current_sudo_reason() -> str | None:
    state = _sudo_state.get()
    return None if state is None else state.get("reason")


# ---------- Subject conversion ----------

class _RebacSubjectMarker(Protocol):
    """Anything decorated with @rebac_subject exposes _rebac_type and _rebac_id_attr."""

    _rebac_type: str
    _rebac_id_attr: str


ActorLike = Union[SubjectRef, _RebacSubjectMarker, Any]
"""Anything resolvable to a `SubjectRef`.

Concretely accepts:
  - `SubjectRef` (passed through)
  - Django `User` instance (→ `auth/user:<pk>`)
  - Django `Group` instance (→ `auth/group:<pk>#member`)
  - Any class decorated with `@rebac_subject(...)` (→ `<type>:<id_attr_value>`)
"""


_subject_registry: dict[type, tuple[str, str]] = {}
"""Mapping `cls -> (rebac_type, id_attr)` populated by `@rebac_subject`."""


def rebac_subject(*, type: str, id_attr: str = "pk") -> Callable[[type], type]:
    """Decorator: register a class as a subject type.

    Example:
        @rebac_subject(type="auth/apikey", id_attr="public_id")
        class ApiKey: ...
    """

    def _decorator(cls: type) -> type:
        _subject_registry[cls] = (type, id_attr)
        cls._rebac_type = type  # type: ignore[attr-defined]
        cls._rebac_id_attr = id_attr  # type: ignore[attr-defined]
        return cls

    return _decorator


def to_subject_ref(actor: ActorLike) -> SubjectRef:
    """Resolve `actor` to a `SubjectRef`. Raises `NoActorResolvedError`."""
    if actor is None:
        raise NoActorResolvedError("Cannot resolve None to a SubjectRef")
    if isinstance(actor, SubjectRef):
        return actor

    from ._id import subject_id_attr

    user_model = get_user_model()
    if isinstance(actor, user_model):
        # AnonymousUser hits a different branch below since AbstractBaseUser
        # is the parent class.
        if not getattr(actor, "is_authenticated", False):
            raise NoActorResolvedError("AnonymousUser cannot be a SubjectRef")
        attr = subject_id_attr(user_model)
        return SubjectRef.of(
            app_settings.REBAC_USER_TYPE, str(getattr(actor, attr))
        )

    # Group?
    try:
        from django.contrib.auth.models import Group
    except ImportError:  # pragma: no cover
        Group = None  # type: ignore[assignment]
    if Group is not None and isinstance(actor, Group):
        attr = subject_id_attr(Group)
        return SubjectRef.of(
            app_settings.REBAC_GROUP_TYPE,
            str(getattr(actor, attr)),
            "member",
        )

    # @rebac_subject-registered?
    for cls, (type_, id_attr) in _subject_registry.items():
        if isinstance(actor, cls):
            value = getattr(actor, id_attr)
            return SubjectRef.of(type_, str(value))

    raise NoActorResolvedError(
        f"Cannot resolve {type(actor).__name__} instance to SubjectRef. "
        f"Decorate the class with @rebac_subject(type=..., id_attr=...) "
        f"or pass a SubjectRef directly."
    )


def grant_subject_ref(agent: Any, on_behalf_of: Any | None) -> SubjectRef:
    """Build a Grant subject for `agent` acting on behalf of `on_behalf_of`.

    The resolution requires the consumer to have registered both an `agents/agent`
    and `agents/grant` subject type via `@rebac_subject`. The plugin itself doesn't
    ship those types — they live in the consumer's `agents` app. We synthesise
    a deterministic grant id from the (agent_id, user_id) pair.

    For systems where grants live in the DB, override
    `REBAC_ACTOR_RESOLVER` to translate (request, agent, user) into the
    persisted grant id.
    """
    agent_ref = to_subject_ref(agent)
    if on_behalf_of is None:
        # Standalone agent run — no user-grant intersection.
        return agent_ref
    user_ref = to_subject_ref(on_behalf_of)
    grant_id = f"{user_ref.subject_id}.{agent_ref.subject_id}"
    return SubjectRef(
        object=ObjectRef("agents/grant", grant_id),
        optional_relation="valid",
    )


# ---------- sudo / system_context ----------

@contextmanager
def actor_context(actor: ActorLike) -> Iterator[None]:
    """Block-scoped ambient actor. Usually you want `.with_actor(actor)` on a
    queryset instead — this is for non-queryset code paths (manual checks).
    """
    ref = to_subject_ref(actor) if not isinstance(actor, SubjectRef) else actor
    token = _current_actor.set(ref)
    try:
        yield
    finally:
        _current_actor.reset(token)


@contextmanager
def sudo(*, reason: str | None = None) -> Iterator[None]:
    """Bypass REBAC checks for the duration of the block.

    `reason` is mandatory unless `REBAC_REQUIRE_SUDO_REASON = False`.
    """
    if not app_settings.REBAC_ALLOW_SUDO:
        raise SudoNotAllowedError("sudo() denied: REBAC_ALLOW_SUDO is False")
    if app_settings.REBAC_REQUIRE_SUDO_REASON and not reason:
        raise SudoReasonRequiredError(
            "sudo() requires a `reason=...` argument when "
            "REBAC_REQUIRE_SUDO_REASON is True"
        )
    state = {"reason": reason or ""}
    token = _sudo_state.set(state)
    try:
        # Audit log emission deferred — wired via signals when available.
        yield
    finally:
        _sudo_state.reset(token)


system_context = sudo  # alias, idiomatic for cron / migrations


# ---------- Default resolver ----------

def default_resolver(request: Any) -> SubjectRef | None:
    """Default `request → SubjectRef` resolver. Used by `ActorMiddleware`.

    Override via `REBAC_ACTOR_RESOLVER = "myapp.path.to.resolver"`.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    try:
        return to_subject_ref(user)
    except NoActorResolvedError:
        return None


def get_actor_resolver() -> Callable[[Any], SubjectRef | None]:
    """Look up the actor resolver from settings."""
    path = app_settings.REBAC_ACTOR_RESOLVER
    module_path, _, attr = path.rpartition(".")
    module = import_module(module_path)
    return getattr(module, attr)
