"""RebacMixin — model-layer enforcement entry point.

`Meta.rebac_resource_type` is recognised via a custom metaclass that pops the
attribute before delegating to Django's `ModelBase` (which would otherwise
reject it as an unknown Meta option). The value is stored as
`<Model>._meta.rebac_resource_type` after class creation, so callers continue
to read it as a Meta attribute even though Django itself doesn't track it.

Non-model classes (views, menus) use `RebacObjectMeta` instead, which stores
the same keys directly on the class as ``_rebac_resource_type`` etc. rather
than on ``_meta``. ``RebacModelBase`` inherits ``RebacObjectMeta`` and only
overrides where the captured values land.
"""

from __future__ import annotations

from typing import Any, Self

from django.db import models
from django.db.models.base import ModelBase

from ._id import resource_id_attr
from .managers import RebacManager
from .types import CheckResult, Consistency, ObjectRef, SubjectRef

_RECOGNISED_META = (
    "rebac_resource_type",
    "rebac_default_action",
    # Per-model override for the attribute the engine reads when
    # building a resource_id (signals + manager) or a subject_id
    # (``to_subject_ref`` for User / Group). Default resolution order
    # is `Meta.rebac_id_attr` → `app_settings.REBAC_RESOURCE_ID_ATTR`
    # → ``"pk"``. See `_id.resource_id_attr`.
    "rebac_id_attr",
)


def _capture_rebac_meta(attrs: dict[str, Any]) -> dict[str, Any]:
    """Pop recognised REBAC keys off ``class Meta:`` and return them.

    Called before ``super().__new__()`` so neither Django's ``ModelBase``
    nor plain ``type`` ever sees the keys.

    Only keys defined directly on this ``Meta`` (i.e. in ``vars(meta)``) are
    deleted; inherited keys are captured by value but left intact on the
    ancestor class. Otherwise a subclass that reuses a parent's ``Meta``
    (legitimate for ``RebacObjectMeta`` views/menus) would mutate the
    ancestor in place — its second instantiation would silently lose the
    attributes the metaclass relies on.
    """
    meta = attrs.get("Meta")
    captured: dict[str, Any] = {}
    if meta is None:
        return captured
    own = vars(meta)
    for key in _RECOGNISED_META:
        if key in own:
            captured[key] = own[key]
            delattr(meta, key)
        elif hasattr(meta, key):
            captured[key] = getattr(meta, key)
    return captured


class RebacObjectMeta(type):
    """Registration metaclass for non-model REBAC resources (views, menus, etc.).

    Captures the same ``_RECOGNISED_META`` keys as ``RebacModelBase`` but
    stores them directly on the class as ``_rebac_<key>`` attributes rather
    than on ``._meta`` (which only exists on Django models).

    Usage::

        class FileListView(ListView):
            class Meta:
                rebac_resource_type = "angee/view"
                rebac_id_attr = "_angee_view_meta.source.operation"

        # After class creation:
        # FileListView._rebac_resource_type == "angee/view"
    """

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        attrs: dict[str, Any],
        **kwargs: Any,
    ) -> type:
        captured = _capture_rebac_meta(attrs)
        new_cls = super().__new__(mcs, name, bases, attrs, **kwargs)
        mcs._store_rebac_meta(new_cls, captured)
        return new_cls

    @staticmethod
    def _store_rebac_meta(cls: type, captured: dict[str, Any]) -> None:
        # Keys are already named "rebac_*", so prefix with "_" only:
        # "rebac_resource_type" → "_rebac_resource_type"
        for key, value in captured.items():
            setattr(cls, f"_{key}", value)


class RebacModelBase(RebacObjectMeta, ModelBase):
    """Custom metaclass that strips ZED-specific Meta attrs before Django sees them.

    Inherits ``RebacObjectMeta`` for the capture logic and overrides
    ``_store_rebac_meta`` to stash values onto ``._meta`` so callers can still
    read them as ``<Model>._meta.rebac_resource_type`` (signals, manager,
    resources.py).

    MRO: RebacModelBase → RebacObjectMeta → ModelBase → type.
    ``super().__new__()`` in ``RebacObjectMeta`` chains through
    ``ModelBase.__new__()`` correctly.
    """

    @staticmethod
    def _store_rebac_meta(cls: type, captured: dict[str, Any]) -> None:
        for key, value in captured.items():
            setattr(cls._meta, key, value)


class RebacMixin(models.Model, metaclass=RebacModelBase):
    """Mix into a model to gate every read / write / delete on REBAC.

    Required: declare `Meta.rebac_resource_type = "<app>/<resource>"`.

    What this installs:
      - `objects = RebacManager()` — replaces the default manager.
      - `_default_manager` points at it; `_base_manager` left unfiltered (Django
        uses base manager for FK reverse caching / M2M intermediates).
      - Pre-save / pre-delete signal handlers gate writes (wired in `signals.py`).
      - `from_db()` override propagates the queryset's actor onto loaded instances.

    Instance-level surface (Odoo PR #179148 triple, plus actor / sudo binding):

      - ``instance.with_actor(actor)`` — pin actor for subsequent
        ``check_access`` / ``has_access`` / ``save()`` / ``delete()`` calls.
        Mirrors the queryset verb; ``as_user`` / ``as_agent`` are sugar
        over it.
      - ``instance.sudo(reason="...")`` — bypass REBAC on this instance only.
        Per CLAUDE.md § 5a the flag is non-transitive: FK / reverse-FK / M2M
        accessors must not inherit it. (Today no accessor reads the flag, so
        the invariant holds vacuously; the v1.x FK-accessor scoping work
        will need to keep ignoring ``_rebac_sudo_reason`` on traversal.)
      - ``instance.is_sudo()`` / ``instance.actor()`` — introspection.
      - ``instance.check_access(action)`` — three-state ``CheckResult``.
      - ``instance.has_access(action)`` — boolean shorthand.
    """

    objects = RebacManager()

    # Carried through from_db (via the queryset's `_fetch_all`) so
    # `instance.save()` re-checks against the same actor.
    _rebac_actor: SubjectRef | None = None
    # TODO(spec): pickle posture — instances may cross HTTP→Celery via
    # ``apply_async(args=[instance])``; whether ``_rebac_sudo_reason``
    # should travel is undecided. Default behaviour today: it pickles.
    # Consumers serialising RebacMixin instances should clear sudo before
    # dispatch. Track in ARCHITECTURE.md "Open questions".
    _rebac_sudo_reason: str | None = None

    class Meta:
        abstract = True

    @classmethod
    def from_db(cls, db: Any, field_names: Any, values: Any) -> RebacMixin:
        return super().from_db(db, field_names, values)

    # ----- Actor / sudo binding -----

    def with_actor(self, actor: Any) -> Self:
        """Pin a SubjectRef on this instance. Returns self for chaining.

        Mirrors ``RebacQuerySet.with_actor`` — accepts any ``ActorLike``.
        Useful for hand-built instances (``Note(...).with_actor(u).save()``)
        that never flowed through a scoped queryset.

        Binding an actor clears any prior ``instance.sudo()`` on this
        instance — sudo and a pinned actor are mutually exclusive intents,
        same contract as the queryset.
        """
        from .actors import to_subject_ref

        self._rebac_actor = actor if isinstance(actor, SubjectRef) else to_subject_ref(actor)
        self._rebac_sudo_reason = None
        return self

    def as_user(self, user: Any) -> Self:
        """Typed shorthand: pin a Django ``User`` as the actor.

        Sugar for ``with_actor(to_subject_ref(user))`` — exactly one code
        path lives in ``with_actor``.
        """
        from .actors import to_subject_ref

        return self.with_actor(to_subject_ref(user))

    def as_agent(self, agent: Any, *, on_behalf_of: Any | None = None) -> Self:
        """Typed shorthand: pin an agent acting via a Grant.

        Sugar for ``with_actor(grant_subject_ref(agent, on_behalf_of))``.
        """
        from .actors import grant_subject_ref

        return self.with_actor(grant_subject_ref(agent, on_behalf_of))

    def sudo(self, *, reason: str) -> Self:
        """Bypass REBAC on this instance. Mandatory ``reason``.

        Scope is **this instance only** (CLAUDE.md § 5a). The bypass
        applies to the next ``save()`` / ``delete()`` / ``check_access``
        on this instance. FK / reverse-FK / M2M accessors do not — and
        must not — inherit the flag.
        """
        from .conf import app_settings
        from .errors import SudoNotAllowedError, SudoReasonRequiredError

        if not app_settings.REBAC_ALLOW_SUDO:
            raise SudoNotAllowedError("sudo() denied: REBAC_ALLOW_SUDO is False")
        if app_settings.REBAC_REQUIRE_SUDO_REASON and not reason:
            raise SudoReasonRequiredError(
                "sudo() requires reason= when REBAC_REQUIRE_SUDO_REASON=True"
            )
        self._rebac_sudo_reason = reason
        return self

    def is_sudo(self) -> bool:
        return self._rebac_sudo_reason is not None

    def actor(self) -> SubjectRef | None:
        """Return the pinned actor on this instance, or ``None``."""
        return self._rebac_actor

    # ----- Check API (Odoo PR #179148 triple) -----

    def check_access(
        self,
        action: str,
        *,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
    ) -> CheckResult:
        """Three-state check for ``action`` on this instance.

        Resolution order:
          1. Model not wired (no ``rebac_resource_type``) → ``HAS_PERMISSION``
             (mirrors the queryset's no-op behaviour).
          2. Per-instance sudo OR ambient ``is_sudo()`` → ``HAS_PERMISSION``.
             Note: ambient sudo overrides a pinned actor at check time —
             same precedence as ``RebacQuerySet._resolve_effective_actor``.
          3. Resolve actor: ``_rebac_actor`` first, then ``current_actor()``.
          4. No actor + strict mode → raise ``MissingActorError``.
          5. Otherwise dispatch to the backend.
        """
        from .actors import current_actor as _current_actor
        from .actors import is_sudo as _is_sudo_ambient
        from .backends import backend
        from .conf import app_settings
        from .errors import MissingActorError

        rebac_type = getattr(type(self)._meta, "rebac_resource_type", None)
        if not rebac_type:
            # Model isn't wired into REBAC — answer permissively to mirror
            # the manager's no-op behaviour.
            return CheckResult.has(reason="no resource type")

        if self._rebac_sudo_reason is not None or _is_sudo_ambient():
            return CheckResult.has(reason="sudo")

        actor = self._rebac_actor or _current_actor()
        if actor is None:
            if app_settings.REBAC_STRICT_MODE:
                raise MissingActorError(
                    f"{type(self).__name__}.check_access({action!r}) called with no actor. "
                    f"Use instance.with_actor(actor) or wrap in `with sudo(reason='...'):`."
                )
            return CheckResult.no(reason="no actor (strict mode off)")

        # Empty resource_id on adding — same sentinel the pre-save signal
        # uses so the backend treats it as a model-level "any row?" check.
        if self._state.adding:
            resource_id = ""
        else:
            resource_id = str(getattr(self, resource_id_attr(type(self))))
        return backend().check_access(
            subject=actor,
            action=action,
            resource=ObjectRef(rebac_type, resource_id),
            context=context,
            consistency=consistency,
        )

    def has_access(
        self,
        action: str,
        *,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
    ) -> bool:
        """Boolean shorthand. ``CONDITIONAL_PERMISSION`` collapses to ``False``."""
        return self.check_access(action, context=context, consistency=consistency).allowed
