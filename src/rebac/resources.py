"""Resource registry: `@rebac_resource` decorator and `to_object_ref` resolver."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .conf import app_settings
from .types import ObjectRef


def _resolve_dotted(obj: Any, attr_path: str) -> Any:
    """Resolve a dotted attribute path against ``obj``.

    ``"a.b.c"`` → ``obj.a.b.c``. Used by ``to_object_ref`` to support
    ``rebac_id_attr = "_angee_view_meta.source.operation"`` on view classes
    registered via ``RebacObjectMeta``.
    """
    value = obj
    for part in attr_path.split("."):
        value = getattr(value, part)
    return value


def _apply_prefix(rebac_type: str) -> str:
    """Prepend ``app_settings.REBAC_TYPE_PREFIX`` to ``rebac_type`` (if set).

    Single point of policy so every branch of :func:`to_object_ref` agrees
    on the wire form. Multi-package deployments rely on prefix isolation;
    a branch that bypassed it would silently emit cross-tenant collisions.
    """
    prefix = app_settings.REBAC_TYPE_PREFIX or ""
    return f"{prefix}{rebac_type}" if prefix else rebac_type


_resource_registry: dict[type, tuple[str, str]] = {}
"""Mapping `cls -> (rebac_type, id_attr)` populated by `@rebac_resource`."""


def rebac_resource(*, type: str, id_attr: str = "pk") -> Callable[[type], type]:
    """Register a class as a known REBAC resource type.

    Lets `to_object_ref(instance)` work without a `Meta.rebac_resource_type`
    binding — useful for plain Python entities (S3 prefixes, queue names,
    anything stable-id'd).
    """

    def _decorator(cls: type) -> type:
        _resource_registry[cls] = (type, id_attr)
        cls._rebac_type = type  # type: ignore[attr-defined]
        cls._rebac_id_attr = id_attr  # type: ignore[attr-defined]
        return cls

    return _decorator


def to_object_ref(obj: Any) -> ObjectRef:
    """Resolve ``obj`` to an :class:`ObjectRef`.

    Lookup precedence (mutually exclusive — a class should declare exactly one):

    1. **Django model** via ``RebacMixin`` — reads
       ``obj._meta.rebac_resource_type``.
    2. **`@rebac_resource` registry** — class explicitly registered.
    3. **`RebacObjectMeta` class** — non-model resources (views, menus) with
       class-level ``_rebac_resource_type`` captured from a ``Meta`` inner
       class by the :class:`~rebac.mixins.RebacObjectMeta` metaclass.
    4. **Legacy duck-typed** — instance exposes ``_rebac_type`` /
       ``_rebac_id_attr`` directly (older ``@rebac_resource`` form).

    All paths apply :data:`app_settings.REBAC_TYPE_PREFIX` so the wire form
    is consistent regardless of how the resource was declared.

    Raises :class:`TypeError` if no path resolves ``obj``.
    """
    # 1. Django model with RebacMixin
    meta = getattr(obj, "_meta", None)
    if meta is not None:
        rebac_type = getattr(meta, "rebac_resource_type", None)
        if rebac_type:
            return ObjectRef(_apply_prefix(rebac_type), str(obj.pk))

    # 2. @rebac_resource registry
    for cls, (type_, id_attr) in _resource_registry.items():
        if isinstance(obj, cls):
            value = getattr(obj, id_attr)
            return ObjectRef(_apply_prefix(type_), str(value))

    # 3. RebacObjectMeta — class-level _rebac_resource_type (views, menus, etc.)
    cls_obj = type(obj)
    resource_type = getattr(cls_obj, "_rebac_resource_type", None)
    if resource_type:
        id_attr = getattr(cls_obj, "_rebac_id_attr", "pk")
        try:
            resource_id = _resolve_dotted(obj, id_attr)
        except AttributeError as exc:
            raise TypeError(
                f"Cannot resolve {cls_obj.__name__} to ObjectRef: "
                f"rebac_id_attr={id_attr!r} not found on instance ({exc})."
            ) from exc
        return ObjectRef(_apply_prefix(resource_type), str(resource_id))

    # 4. Legacy duck-typed (instance exposes the markers directly)
    type_ = getattr(obj, "_rebac_type", None)
    id_attr = getattr(obj, "_rebac_id_attr", None)
    if type_ and id_attr:
        value = getattr(obj, id_attr)
        return ObjectRef(_apply_prefix(type_), str(value))

    raise TypeError(
        f"Cannot resolve {type(obj).__name__} to ObjectRef. "
        f"Add Meta.rebac_resource_type, decorate with @rebac_resource, "
        f"or pass an ObjectRef directly."
    )
