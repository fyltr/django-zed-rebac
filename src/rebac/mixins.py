"""RebacMixin — model-layer enforcement entry point.

`Meta.rebac_resource_type` is recognised via a custom metaclass that pops the
attribute before delegating to Django's `ModelBase` (which would otherwise
reject it as an unknown Meta option). The value is stored as
`<Model>._meta.rebac_resource_type` after class creation, so callers continue
to read it as a Meta attribute even though Django itself doesn't track it.

Non-model classes (views, menus) use `RebacObjectMeta` instead, which stores
the same keys directly on the class as ``_rebac_resource_type`` etc. rather
than on ``_meta``. ``RebacModelBase`` inherits ``RebacObjectMeta`` and only
overrides where the captured values land.
"""

from __future__ import annotations

from typing import Any

from django.db import models
from django.db.models.base import ModelBase

from .managers import RebacManager

_RECOGNISED_META = (
    "rebac_resource_type",
    "rebac_default_action",
    # Per-model override for the attribute the engine reads when
    # building a resource_id (signals + manager) or a subject_id
    # (``to_subject_ref`` for User / Group). Default resolution order
    # is `Meta.rebac_id_attr` → `app_settings.REBAC_RESOURCE_ID_ATTR`
    # → ``"pk"``. See `_id.resource_id_attr`.
    "rebac_id_attr",
)


def _capture_rebac_meta(attrs: dict[str, Any]) -> dict[str, Any]:
    """Pop recognised REBAC keys off ``class Meta:`` and return them.

    Called before ``super().__new__()`` so neither Django's ``ModelBase``
    nor plain ``type`` ever sees the keys.

    Only keys defined directly on this ``Meta`` (i.e. in ``vars(meta)``) are
    deleted; inherited keys are captured by value but left intact on the
    ancestor class. Otherwise a subclass that reuses a parent's ``Meta``
    (legitimate for ``RebacObjectMeta`` views/menus) would mutate the
    ancestor in place — its second instantiation would silently lose the
    attributes the metaclass relies on.
    """
    meta = attrs.get("Meta")
    captured: dict[str, Any] = {}
    if meta is None:
        return captured
    own = vars(meta)
    for key in _RECOGNISED_META:
        if key in own:
            captured[key] = own[key]
            delattr(meta, key)
        elif hasattr(meta, key):
            captured[key] = getattr(meta, key)
    return captured


class RebacObjectMeta(type):
    """Registration metaclass for non-model REBAC resources (views, menus, etc.).

    Captures the same ``_RECOGNISED_META`` keys as ``RebacModelBase`` but
    stores them directly on the class as ``_rebac_<key>`` attributes rather
    than on ``._meta`` (which only exists on Django models).

    Usage::

        class FileListView(ListView):
            class Meta:
                rebac_resource_type = "angee/view"
                rebac_id_attr = "_angee_view_meta.source.operation"

        # After class creation:
        # FileListView._rebac_resource_type == "angee/view"
    """

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        attrs: dict[str, Any],
        **kwargs: Any,
    ) -> type:
        captured = _capture_rebac_meta(attrs)
        new_cls = super().__new__(mcs, name, bases, attrs, **kwargs)
        mcs._store_rebac_meta(new_cls, captured)
        return new_cls

    @staticmethod
    def _store_rebac_meta(cls: type, captured: dict[str, Any]) -> None:
        # Keys are already named "rebac_*", so prefix with "_" only:
        # "rebac_resource_type" → "_rebac_resource_type"
        for key, value in captured.items():
            setattr(cls, f"_{key}", value)


class RebacModelBase(RebacObjectMeta, ModelBase):
    """Custom metaclass that strips ZED-specific Meta attrs before Django sees them.

    Inherits ``RebacObjectMeta`` for the capture logic and overrides
    ``_store_rebac_meta`` to stash values onto ``._meta`` so callers can still
    read them as ``<Model>._meta.rebac_resource_type`` (signals, manager,
    resources.py).

    MRO: RebacModelBase → RebacObjectMeta → ModelBase → type.
    ``super().__new__()`` in ``RebacObjectMeta`` chains through
    ``ModelBase.__new__()`` correctly.
    """

    @staticmethod
    def _store_rebac_meta(cls: type, captured: dict[str, Any]) -> None:
        for key, value in captured.items():
            setattr(cls._meta, key, value)


class RebacMixin(models.Model, metaclass=RebacModelBase):
    """Mix into a model to gate every read / write / delete on REBAC.

    Required: declare `Meta.rebac_resource_type = "<app>/<resource>"`.

    What this installs:
      - `objects = RebacManager()` — replaces the default manager.
      - `_default_manager` points at it; `_base_manager` left unfiltered (Django
        uses base manager for FK reverse caching / M2M intermediates).
      - Pre-save / pre-delete signal handlers gate writes (wired in `signals.py`).
      - `from_db()` override propagates the queryset's actor onto loaded
        instances and snapshots loaded field values into
        ``_rebac_loaded_values`` for later dirty-field computation
        (per-field ``write__<f>`` enforcement; see ``signals.py``).

    Pickle / cross-process posture:
      ``__getstate__`` strips ``_rebac_actor``, ``_rebac_sudo_reason``, and
      ``_rebac_loaded_values`` before pickling. Instances crossing
      process / wire boundaries (e.g. ``apply_async(args=[instance])``)
      lose their pinned actor and sudo binding; the receiving worker MUST
      re-attach an actor via middleware / Celery hook before saving. This
      is fail-closed by design — actor identity must be re-asserted at
      every trust boundary, never silently inherited from a serialised
      blob.
    """

    objects = RebacManager()

    # Carried through from_db so `instance.save()` re-checks against the same actor.
    _rebac_actor: Any = None

    class Meta:
        abstract = True

    @classmethod
    def from_db(cls, db: Any, field_names: Any, values: Any) -> RebacMixin:
        """Instantiate from a row + snapshot loaded field values.

        The snapshot lives on ``instance._rebac_loaded_values`` and powers
        per-field write gates: ``signals.py`` compares it against the
        current values to detect dirty fields, then re-checks each one
        against ``write__<field>`` if such a permission is declared.

        Deferred fields are skipped (they aren't in ``field_names``); a
        subsequent ``refresh_from_db`` does NOT update the snapshot — that
        would defeat the audit purpose of "what was on the row when the
        actor first loaded it". Pure in-memory; no extra queries.
        """
        from django.db.models import DEFERRED

        instance = super().from_db(db, field_names, values)
        # field_names is parallel to values; both are the attnames Django loaded.
        snapshot: dict[str, Any] = {}
        for name, value in zip(field_names, values, strict=False):
            if value is DEFERRED:
                continue
            snapshot[name] = value
        instance._rebac_loaded_values = snapshot
        return instance

    def __getstate__(self) -> dict[str, Any]:
        """Strip per-instance REBAC binding before pickling.

        Removes ``_rebac_actor``, ``_rebac_sudo_reason``, and
        ``_rebac_loaded_values`` so a pickled instance cannot smuggle a
        trusted actor across a process boundary. The receiving end must
        re-attach an actor via middleware / Celery hook (see
        ``CLAUDE.md § 5`` — actor lives on the queryset / instance, not
        in a ContextVar that survives pickling).
        """
        state = super().__getstate__()
        if isinstance(state, dict):
            state.pop("_rebac_actor", None)
            state.pop("_rebac_sudo_reason", None)
            state.pop("_rebac_loaded_values", None)
        return state
