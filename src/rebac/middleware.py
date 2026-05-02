"""ActorMiddleware — populates `current_actor()` from `request.user`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .actors import _current_actor, get_actor_resolver


class ActorMiddleware:
    """Reads `request.user` (via the configured resolver) and sets the
    `current_actor()` ContextVar for the duration of the request.

    Add to MIDDLEWARE *after* `AuthenticationMiddleware`.
    """

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        resolver = get_actor_resolver()
        actor_ref = resolver(request)
        token = _current_actor.set(actor_ref)
        try:
            response = self.get_response(request)
        finally:
            _current_actor.reset(token)
        return response
