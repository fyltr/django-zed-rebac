"""ZedRBACMixin — model-layer enforcement entry point.

`Meta.zed_resource_type` is recognised via a custom metaclass that pops the
attribute before delegating to Django's `ModelBase` (which would otherwise
reject it as an unknown Meta option). The value is stored as
`<Model>._meta.zed_resource_type` after class creation, so callers continue
to read it as a Meta attribute even though Django itself doesn't track it.
"""
from __future__ import annotations

from typing import Any

from django.db import models
from django.db.models.base import ModelBase

from .managers import ZedRBACManager


_RECOGNISED_META = ("zed_resource_type", "zed_default_action")


class ZedRBACModelBase(ModelBase):
    """Custom metaclass that strips ZED-specific Meta attrs before Django sees them."""

    def __new__(mcs, name: str, bases: tuple, attrs: dict, **kwargs: Any) -> type:
        meta = attrs.get("Meta")
        captured: dict[str, Any] = {}
        if meta is not None:
            for key in _RECOGNISED_META:
                if hasattr(meta, key):
                    captured[key] = getattr(meta, key)
                    delattr(meta, key)
        new_cls = super().__new__(mcs, name, bases, attrs, **kwargs)
        # Stash captured values onto _meta so callers can still read them as
        # `<Model>._meta.zed_resource_type` (signals, manager, resources.py).
        for key, value in captured.items():
            setattr(new_cls._meta, key, value)
        return new_cls


class ZedRBACMixin(models.Model, metaclass=ZedRBACModelBase):
    """Mix into a model to gate every read / write / delete on REBAC.

    Required: declare `Meta.zed_resource_type = "<app>/<resource>"`.

    What this installs:
      - `objects = ZedRBACManager()` — replaces the default manager.
      - `_default_manager` points at it; `_base_manager` left unfiltered (Django
        uses base manager for FK reverse caching / M2M intermediates).
      - Pre-save / pre-delete signal handlers gate writes (wired in `signals.py`).
      - `from_db()` override propagates the queryset's actor onto loaded instances.
    """

    objects = ZedRBACManager()

    # Carried through from_db so `instance.save()` re-checks against the same actor.
    _zed_actor: Any = None

    class Meta:
        abstract = True

    @classmethod
    def from_db(cls, db: Any, field_names: Any, values: Any) -> "ZedRBACMixin":
        return super().from_db(db, field_names, values)
