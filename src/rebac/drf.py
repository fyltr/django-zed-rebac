"""DRF integration: RebacPermission + RebacFilterBackend.

Soft-imports `rest_framework` so the module can be imported without DRF
installed. Importing the names from this module raises `ImportError` only
when DRF is missing AND the names are actually used.
"""

from __future__ import annotations

from typing import Any

try:
    from rest_framework import filters as _drf_filters
    from rest_framework import permissions as _drf_perms

    _HAS_DRF = True
except ImportError:  # pragma: no cover
    _drf_perms = None  # type: ignore[assignment]
    _drf_filters = None  # type: ignore[assignment]
    _HAS_DRF = False


_DEFAULT_ACTION_MAP = {
    "list": "read",
    "retrieve": "read",
    "create": "create",
    "update": "write",
    "partial_update": "write",
    "destroy": "delete",
}


if _HAS_DRF:

    class RebacPermission(_drf_perms.BasePermission):  # type: ignore[misc]
        """Routes per-action permission through `backend().has_access`.

        Override `action_map` to customise:
            class MyPerm(RebacPermission):
                action_map = {**RebacPermission.action_map, "publish": "publish"}
        """

        action_map = _DEFAULT_ACTION_MAP

        def has_permission(self, request: Any, view: Any) -> bool:
            from . import backend
            from .actors import current_actor, to_subject_ref
            from .errors import NoActorResolvedError

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

            from .types import ObjectRef

            # Model-level check (empty resource_id) for create/list.
            return backend().has_access(
                subject=subject,
                action=rebac_action,
                resource=ObjectRef(rebac_type, ""),
            )

        def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
            from . import backend
            from .actors import current_actor, to_subject_ref
            from .errors import NoActorResolvedError
            from .resources import to_object_ref

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

    class RebacFilterBackend(_drf_filters.BaseFilterBackend):  # type: ignore[misc]
        """Scopes a viewset's queryset to the actor."""

        def filter_queryset(self, request: Any, queryset: Any, view: Any) -> Any:
            user = getattr(request, "user", None)
            if not user or not getattr(user, "is_authenticated", False):
                return queryset.none()
            if not hasattr(queryset, "as_user"):
                return queryset
            return queryset.as_user(user)

else:  # pragma: no cover

    class RebacPermission:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "RebacPermission requires djangorestframework. pip install django-zed-rebac[drf]"
            )

    class RebacFilterBackend:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "RebacFilterBackend requires djangorestframework. pip install django-zed-rebac[drf]"
            )
