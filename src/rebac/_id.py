"""Resource / subject id-attribute resolution.

Single source of truth for *which* attribute the engine reads when
building a resource_id (signals + manager) or a subject_id (the
``to_subject_ref`` Django-User / Group branches).

Resolution order, narrowest to broadest:

1. Per-model ``Meta.rebac_id_attr`` — recognised by
   :class:`RebacModelBase` and re-attached onto ``cls._meta``.
2. The corresponding global setting (``REBAC_RESOURCE_ID_ATTR`` for
   resources, ``REBAC_USER_ID_ATTR`` for the actor side).
3. ``"pk"`` — the historical default; kept so existing consumers
   behave identically without opt-in.
"""

from __future__ import annotations

from typing import Any

from .conf import app_settings


def resource_id_attr(model_cls: Any) -> str:
    """Return the attribute name used to source a resource's id."""
    attr = getattr(model_cls._meta, "rebac_id_attr", None)
    return attr or app_settings.REBAC_RESOURCE_ID_ATTR


def subject_id_attr(model_cls: Any) -> str:
    """Return the attribute name used to source a subject's id.

    Differs from :func:`resource_id_attr` only in the global fallback —
    actor-side resolution falls through to ``REBAC_USER_ID_ATTR`` so a
    consumer can flip resources without flipping subjects (or vice
    versa). Per-model ``Meta.rebac_id_attr`` still wins on either side.
    """
    attr = getattr(model_cls._meta, "rebac_id_attr", None)
    return attr or app_settings.REBAC_USER_ID_ATTR


__all__ = ["resource_id_attr", "subject_id_attr"]
