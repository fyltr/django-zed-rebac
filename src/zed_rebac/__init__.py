"""django-zed-rebac — drop-in REBAC engine for Django.

See docs/SPEC.md for the design contract. The Python module is `zed_rebac`
(per the naming question in CLAUDE.md, choosing the lean `zed_rebac` form
that matches Django convention: hyphens → underscores).

Note: `ZedRBACMixin` is exposed via `__getattr__` so that this package can be
imported during Django app loading without tripping the model metaclass before
the apps registry is ready. Use `from zed_rebac import ZedRBACMixin` from
*application code* (after Django setup); inside another package's models.py
that's the supported import.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0a0"

default_app_config = "zed_rebac.apps.ZedRebacConfig"

from .actors import (
    ActorLike,
    actor_context,
    current_actor,
    grant_subject_ref,
    set_current_actor,
    sudo,
    system_context,
    to_subject_ref,
    zed_subject,
)
from .conf import app_settings
from .errors import (
    CaveatUnsupportedError,
    MissingActorError,
    NoActorResolvedError,
    PermissionDenied,
    PermissionDepthExceeded,
    SchemaError,
    ZedRebacError,
)
from .types import (
    CheckResult,
    Consistency,
    ObjectRef,
    PermissionResult,
    RelationshipTuple,
    SubjectRef,
    Zookie,
)

if TYPE_CHECKING:
    from .backends import Backend, LocalBackend, SpiceDBBackend
    from .mixins import ZedRBACMixin
    from .decorators import require_permission, zed_resource
    from .relationships import delete_relationships, write_relationships
    from .resources import to_object_ref


_LAZY = {
    "ZedRBACMixin": ("zed_rebac.mixins", "ZedRBACMixin"),
    "Backend": ("zed_rebac.backends", "Backend"),
    "LocalBackend": ("zed_rebac.backends", "LocalBackend"),
    "SpiceDBBackend": ("zed_rebac.backends", "SpiceDBBackend"),
    "backend": ("zed_rebac.backends", "backend"),
    "require_permission": ("zed_rebac.decorators", "require_permission"),
    "zed_resource": ("zed_rebac.decorators", "zed_resource"),
    "write_relationships": ("zed_rebac.relationships", "write_relationships"),
    "delete_relationships": ("zed_rebac.relationships", "delete_relationships"),
    "to_object_ref": ("zed_rebac.resources", "to_object_ref"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'zed_rebac' has no attribute {name!r}")
    from importlib import import_module
    module = import_module(target[0])
    return getattr(module, target[1])

__all__ = [
    "__version__",
    # types
    "ObjectRef",
    "SubjectRef",
    "CheckResult",
    "PermissionResult",
    "Consistency",
    "Zookie",
    "RelationshipTuple",
    # mixin / managers
    "ZedRBACMixin",
    # decorators
    "require_permission",
    "zed_resource",
    "zed_subject",
    # backends
    "Backend",
    "LocalBackend",
    "SpiceDBBackend",
    "backend",
    # actors
    "ActorLike",
    "current_actor",
    "set_current_actor",
    "actor_context",
    "sudo",
    "system_context",
    "to_subject_ref",
    "grant_subject_ref",
    "to_object_ref",
    # errors
    "PermissionDenied",
    "MissingActorError",
    "CaveatUnsupportedError",
    "PermissionDepthExceeded",
    "NoActorResolvedError",
    "SchemaError",
    "ZedRebacError",
    # helpers
    "write_relationships",
    "delete_relationships",
    # settings
    "app_settings",
]
