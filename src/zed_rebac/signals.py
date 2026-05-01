"""Pre-save / pre-delete signal handlers gating writes through REBAC."""
from __future__ import annotations

from typing import Any

from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver

from .actors import current_actor as _current_actor
from .actors import is_sudo as _is_sudo
from .conf import app_settings
from .errors import MissingActorError, PermissionDenied
from .mixins import ZedRBACMixin
from .types import ObjectRef


@receiver(pre_save)
def _zed_pre_save(sender: type, instance: Any, raw: bool = False, using: Any = None, **_: Any) -> None:
    if raw:
        return
    if not isinstance(instance, ZedRBACMixin):
        return
    zed_type = getattr(sender._meta, "zed_resource_type", None)
    if not zed_type:
        return
    if _is_sudo():
        return

    # Resolve actor: per-instance (set by from_db / queryset) → ambient.
    actor = getattr(instance, "_zed_actor", None) or _current_actor()
    if actor is None:
        if app_settings.ZED_REBAC_STRICT_MODE:
            raise MissingActorError(
                f"{sender.__name__}.save() called with no actor. "
                f"Use a queryset scoped via .with_actor()/.as_user()/.as_agent(), "
                f"or wrap in `with sudo(reason='...'):`."
            )
        return

    is_create = instance._state.adding
    action = "create" if is_create else "write"

    from .backends import backend
    resource = ObjectRef(zed_type, "" if is_create else str(instance.pk))
    result = backend().check_access(subject=actor, action=action, resource=resource)
    if not result.allowed:
        raise PermissionDenied(
            f"Denied: {actor} cannot {action} {resource}"
        )


@receiver(pre_delete)
def _zed_pre_delete(sender: type, instance: Any, using: Any = None, **_: Any) -> None:
    if not isinstance(instance, ZedRBACMixin):
        return
    zed_type = getattr(sender._meta, "zed_resource_type", None)
    if not zed_type:
        return
    if _is_sudo():
        return

    actor = getattr(instance, "_zed_actor", None) or _current_actor()
    if actor is None:
        if app_settings.ZED_REBAC_STRICT_MODE:
            raise MissingActorError(
                f"{sender.__name__}.delete() called with no actor."
            )
        return

    from .backends import backend
    resource = ObjectRef(zed_type, str(instance.pk))
    result = backend().check_access(subject=actor, action="delete", resource=resource)
    if not result.allowed:
        raise PermissionDenied(f"Denied: {actor} cannot delete {resource}")
