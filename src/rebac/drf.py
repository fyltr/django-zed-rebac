"""DRF integration: RebacPermission + RebacFilterBackend.

Requires ``djangorestframework`` (the ``[drf]`` extra). This module is
never imported by ``rebac/__init__.py`` — only consumers wiring the DRF
integration import it, and they are expected to have DRF installed. A
missing dependency therefore surfaces as a plain ``ImportError`` naming
``rest_framework`` at the point of import, which is the correct
fail-fast behaviour.
"""

from __future__ import annotations

from typing import Any

from rest_framework.filters import BaseFilterBackend
from rest_framework.permissions import BasePermission

from .actors import current_actor, to_subject_ref
from .backends import backend
from .errors import NoActorResolvedError
from .resources import to_object_ref
from .types import ObjectRef

_DEFAULT_ACTION_MAP = {
    "list": "read",
    "retrieve": "read",
    "create": "create",
    "update": "write",
    "partial_update": "write",
    "destroy": "delete",
}


class RebacPermission(BasePermission):  # type: ignore[misc]  # untyped third-party base
    """Routes per-action permission through `backend().has_access`.

    Override `action_map` to customise:
        class MyPerm(RebacPermission):
            action_map = {**RebacPermission.action_map, "publish": "publish"}
    """

    action_map = _DEFAULT_ACTION_MAP

    def has_permission(self, request: Any, view: Any) -> bool:
        action_name = getattr(view, "action", None) or request.method.lower()
        rebac_action = self.action_map.get(action_name)
        if rebac_action is None:
            return True

        user = getattr(request, "user", None)
        try:
            subject = to_subject_ref(user) if user else None
        except NoActorResolvedError:
            subject = None
        if subject is None:
            subject = current_actor()
        if subject is None:
            return False

        model_cls = getattr(getattr(view, "queryset", None), "model", None)
        rebac_type = getattr(getattr(model_cls, "_meta", None), "rebac_resource_type", None)
        if not rebac_type:
            return True

        # Model-level check (empty resource_id) for create/list.
        return backend().has_access(
            subject=subject,
            action=rebac_action,
            resource=ObjectRef(rebac_type, ""),
        )

    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        action_name = getattr(view, "action", None) or request.method.lower()
        rebac_action = self.action_map.get(action_name)
        if rebac_action is None:
            return True

        user = getattr(request, "user", None)
        try:
            subject = to_subject_ref(user) if user else None
        except NoActorResolvedError:
            subject = None
        if subject is None:
            subject = current_actor()
        if subject is None:
            return False

        try:
            resource = to_object_ref(obj)
        except TypeError:
            return True
        return backend().has_access(subject=subject, action=rebac_action, resource=resource)


class RebacFilterBackend(BaseFilterBackend):  # type: ignore[misc]  # untyped third-party base
    """Scopes a viewset's queryset to the actor."""

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> Any:
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return queryset.none()
        if not hasattr(queryset, "as_user"):
            return queryset
        return queryset.as_user(user)
