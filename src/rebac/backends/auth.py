"""RebacBackend — Django auth backend that routes per-object `has_perm` through REBAC."""
from __future__ import annotations

from typing import Any

from ..codenames import codename_to_action
from ..conf import app_settings


class RebacBackend:
    """Adds per-object permission checks routed through the REBAC engine.

    Add to AUTHENTICATION_BACKENDS *before* `ModelBackend`. Does not authenticate
    (returns None from `authenticate()`), so it composes with whatever username/
    password / OAuth backend the project uses.
    """

    def authenticate(self, request: Any, **credentials: Any) -> None:
        return None

    def has_perm(self, user_obj: Any, perm: str, obj: Any = None) -> bool:
        if not getattr(user_obj, "is_active", False):
            return False
        if obj is None:
            return False
        if app_settings.REBAC_SUPERUSER_BYPASS and getattr(user_obj, "is_superuser", False):
            return True
        action = codename_to_action(perm)
        if action is None:
            return False

        from . import backend
        from ..actors import to_subject_ref
        from ..errors import NoActorResolvedError
        from ..resources import to_object_ref

        try:
            subject = to_subject_ref(user_obj)
            resource = to_object_ref(obj)
        except (NoActorResolvedError, TypeError):
            return False

        return backend().has_access(subject=subject, action=action, resource=resource)

    def has_module_perms(self, user_obj: Any, app_label: str) -> bool:
        return False

    def get_user(self, user_id: int) -> Any:
        # Authentication is delegated to other backends; this backend only
        # checks permissions. Returning None lets the auth pipeline ignore us.
        return None
