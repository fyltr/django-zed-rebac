"""Resource registry: `@rebac_resource` decorator and `to_object_ref` resolver."""
from __future__ import annotations

from typing import Any, Callable

from .conf import app_settings
from .types import ObjectRef


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
    """Resolve `obj` to an `ObjectRef`.

    Lookup order:
      1. Django Model with `Meta.rebac_resource_type` (set via `RebacMixin`).
      2. Class registered via `@rebac_resource`.
      3. Anything with `_rebac_type` and `_rebac_id_attr` attributes.
    """
    # Django Model path
    meta = getattr(obj, "_meta", None)
    if meta is not None:
        rebac_type = getattr(meta, "rebac_resource_type", None)
        if rebac_type:
            prefix = app_settings.REBAC_TYPE_PREFIX or ""
            full_type = f"{prefix}{rebac_type}" if prefix else rebac_type
            return ObjectRef(full_type, str(obj.pk))

    # Registry / decorator path
    for cls, (type_, id_attr) in _resource_registry.items():
        if isinstance(obj, cls):
            value = getattr(obj, id_attr)
            return ObjectRef(type_, str(value))

    # Fallback: object exposes the markers directly
    type_ = getattr(obj, "_rebac_type", None)
    id_attr = getattr(obj, "_rebac_id_attr", None)
    if type_ and id_attr:
        value = getattr(obj, id_attr)
        return ObjectRef(type_, str(value))

    raise TypeError(
        f"Cannot resolve {type(obj).__name__} to ObjectRef. "
        f"Add Meta.rebac_resource_type, decorate with @rebac_resource, "
        f"or pass an ObjectRef directly."
    )
