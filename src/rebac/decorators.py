"""Public decorators."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from .actors import current_actor, is_sudo
from .errors import NoActorResolvedError, PermissionDenied
from .resources import rebac_resource as _rebac_resource_register
from .resources import to_object_ref
from .types import ObjectRef

rebac_resource = _rebac_resource_register


def require_permission(
    action: str,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    resource_arg: str | None = None,
    actor_arg: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Gate a callable on a REBAC permission.

    Usage:
        @require_permission("invoke",
            resource_type="celery/task/reindex",
            resource_id="*")
        @shared_task
        def reindex(): ...

    Or, for callables that receive an instance:
        @require_permission("write", resource_arg="post")
        def edit(post): ...

    The actor resolves from `current_actor()` unless `actor_arg` names a kwarg.
    """

    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            from . import backend

            if is_sudo():
                return fn(*args, **kwargs)

            # Resolve actor.
            if actor_arg and actor_arg in kwargs:
                from .actors import to_subject_ref

                actor_ref = to_subject_ref(kwargs[actor_arg])
            else:
                actor_ref = current_actor()
            if actor_ref is None:
                raise NoActorResolvedError(
                    f"@require_permission({action!r}) called with no actor in scope"
                )

            # Resolve resource.
            if resource_arg:
                obj = kwargs.get(resource_arg)
                if obj is None:
                    # positional? assume first non-self arg.
                    obj = args[0] if args else None
                if obj is None:
                    raise ValueError(f"resource_arg {resource_arg!r} produced no value")
                resource = to_object_ref(obj)
            elif resource_type is not None:
                resource = ObjectRef(resource_type, resource_id or "")
            else:
                raise ValueError(
                    "@require_permission requires either resource_type=... or resource_arg=..."
                )

            result = backend().check_access(subject=actor_ref, action=action, resource=resource)
            if not result.allowed:
                raise PermissionDenied(f"Denied: {actor_ref} cannot {action} {resource}")
            return fn(*args, **kwargs)

        return _wrapped

    return _decorator
