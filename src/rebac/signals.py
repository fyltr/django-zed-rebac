"""Pre-save / pre-delete signal handlers gating writes through REBAC."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
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
    if _is_sudo():
        return

    # Resolve actor: per-instance (set by from_db / queryset) → ambient.
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
    if _is_sudo():
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


# ---------------------------------------------------------------------------
# SchemaOverride invalidation + audit
# ---------------------------------------------------------------------------
#
# Tier-2 override CRUD must reset the cached backend so the next permission
# check rebuilds the in-memory schema with composition applied. Each create
# or delete also writes a PermissionAuditEvent. Single-process only —
# multi-process LISTEN/NOTIFY is a v1.x roadmap item.


def _override_target_repr(instance: Any) -> str:
    try:
        return f"{instance.kind}:{instance.target_ct.app_label}.{instance.target_ct.model}/{instance.target_pk}"
    except Exception:
        return f"{getattr(instance, 'kind', '?')}:?/{getattr(instance, 'target_pk', '?')}"


def _override_payload(instance: Any) -> dict[str, Any]:
    return {
        "kind": getattr(instance, "kind", ""),
        "expression": getattr(instance, "expression", ""),
        "reason": getattr(instance, "reason", ""),
    }


def _emit_override_audit(*, kind: str, instance: Any, before: Any, after: Any) -> None:
    """Write an override.* PermissionAuditEvent. Deferred to commit."""
    # Local imports — avoid circulars and keep app boot light.
    from .models import PermissionAuditEvent

    actor = _current_actor()
    actor_type = actor.subject_type if actor else ""
    actor_id = actor.subject_id if actor else ""
    target_repr = _override_target_repr(instance)
    reason = getattr(instance, "reason", "") or ""

    def _do_create() -> None:
        PermissionAuditEvent.objects.create(
            kind=kind,
            actor_subject_type=actor_type,
            actor_subject_id=actor_id,
            target_repr=target_repr,
            before=before,
            after=after,
            reason=reason,
        )

    # Best-effort defer to commit; outside an atomic block on_commit fires
    # immediately. If something later in the test rolls back the outer
    # transaction the audit row goes with it — that's the right semantics
    # (no audit for events that didn't happen).
    try:
        transaction.on_commit(_do_create)
    except Exception:
        # No DB / no connection — emit synchronously.
        _do_create()


@receiver(post_save, sender="rebac.SchemaOverride")
def _rebac_override_post_save(
    sender: type, instance: Any, created: bool, raw: bool = False, **_: Any
) -> None:
    if raw:
        return
    # Reset the cached backend so the next check picks up the new override.
    from .backends import reset_backend

    reset_backend()

    from .models import PermissionAuditEvent

    if created:
        _emit_override_audit(
            kind=PermissionAuditEvent.KIND_OVERRIDE_CREATE,
            instance=instance,
            before=None,
            after=_override_payload(instance),
        )
    # Updates to existing overrides aren't separately audited in v1; treat
    # them as configuration drift and rely on the underlying admin log.


@receiver(post_delete, sender="rebac.SchemaOverride")
def _rebac_override_post_delete(sender: type, instance: Any, **_: Any) -> None:
    from .backends import reset_backend

    reset_backend()

    from .models import PermissionAuditEvent

    _emit_override_audit(
        kind=PermissionAuditEvent.KIND_OVERRIDE_DELETE,
        instance=instance,
        before=_override_payload(instance),
        after=None,
    )
