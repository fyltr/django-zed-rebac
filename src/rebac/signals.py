"""Pre-save / pre-delete signal handlers gating writes through REBAC."""

from __future__ import annotations

from typing import Any

from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver

from ._id import resource_id_attr
from .actors import current_actor as _current_actor
from .actors import is_sudo as _is_sudo
from .conf import app_settings
from .errors import MissingActorError, PermissionDenied
from .mixins import RebacMixin
from .types import ObjectRef


@receiver(pre_save)
def _rebac_pre_save(
    sender: type, instance: Any, raw: bool = False, using: Any = None, **_: Any
) -> None:
    if raw:
        return
    if not isinstance(instance, RebacMixin):
        return
    rebac_type = getattr(sender._meta, "rebac_resource_type", None)
    if not rebac_type:
        return
    # Per-instance sudo (set by `instance.sudo(reason=...)`) bypasses the
    # check just like the ambient ContextVar. Per CLAUDE.md § 5a, the flag
    # is non-transitive — it lives on this instance only and does not
    # propagate to FK / M2M accessors.
    if _is_sudo() or getattr(instance, "_rebac_sudo_reason", None) is not None:
        return

    # Resolve actor: per-instance (set by from_db / queryset / .with_actor) → ambient.
    actor = getattr(instance, "_rebac_actor", None) or _current_actor()
    if actor is None:
        if app_settings.REBAC_STRICT_MODE:
            raise MissingActorError(
                f"{sender.__name__}.save() called with no actor. "
                f"Use a queryset scoped via .with_actor()/.as_user()/.as_agent(), "
                f"or wrap in `with sudo(reason='...'):`."
            )
        return

    is_create = instance._state.adding
    action = "create" if is_create else "write"

    from .backends import backend

    # Empty resource_id on create — even when the configured attr is
    # something like ``sqid`` (a virtual field computed from PK), the
    # value isn't computable until after the insert. Same sentinel as
    # the pk-default path.
    if is_create:
        resource_id = ""
    else:
        resource_id = str(getattr(instance, resource_id_attr(sender)))
    resource = ObjectRef(rebac_type, resource_id)
    result = backend().check_access(subject=actor, action=action, resource=resource)
    if not result.allowed:
        raise PermissionDenied(f"Denied: {actor} cannot {action} {resource}")


@receiver(pre_delete)
def _rebac_pre_delete(sender: type, instance: Any, using: Any = None, **_: Any) -> None:
    if not isinstance(instance, RebacMixin):
        return
    rebac_type = getattr(sender._meta, "rebac_resource_type", None)
    if not rebac_type:
        return
    if _is_sudo() or getattr(instance, "_rebac_sudo_reason", None) is not None:
        return

    actor = getattr(instance, "_rebac_actor", None) or _current_actor()
    if actor is None:
        if app_settings.REBAC_STRICT_MODE:
            raise MissingActorError(f"{sender.__name__}.delete() called with no actor.")
        return

    from .backends import backend

    resource_id = str(getattr(instance, resource_id_attr(sender)))
    resource = ObjectRef(rebac_type, resource_id)
    result = backend().check_access(subject=actor, action="delete", resource=resource)
    if not result.allowed:
        raise PermissionDenied(f"Denied: {actor} cannot delete {resource}")
