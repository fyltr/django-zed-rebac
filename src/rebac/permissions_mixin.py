"""``RebacPermissionsMixin`` — a slim drop-in for contrib.auth's
:class:`PermissionsMixin` that doesn't ship Permission / Group M2Ms.

REBAC replaces Django's permissions table wholesale: per-row grants
live in the ``rebac.Relationship`` table, not in
``auth.User.user_permissions`` or ``auth.Group.permissions``. Domain
projects that use REBAC as the only source of truth still need
``has_perm`` / ``has_perms`` / ``has_module_perms`` on their User
model — Django admin, DRF's permission classes, and template tags
all call those methods.

This mixin provides them. The methods walk
:setting:`AUTHENTICATION_BACKENDS` exactly the way contrib.auth's
``PermissionsMixin`` does, but it carries no M2M fields, so it can
ride on a custom user that subclasses :class:`AbstractBaseUser`
directly (instead of the heavier :class:`AbstractUser`) without
forcing ``django.contrib.auth`` into ``INSTALLED_APPS``.

Usage::

    from rebac.permissions_mixin import RebacPermissionsMixin
    from django.contrib.auth.base_user import AbstractBaseUser

    class User(AbstractBaseUser, RebacPermissionsMixin):
        ...

Pair with :class:`rebac.backends.auth.RebacBackend` (front-of-line in
``AUTHENTICATION_BACKENDS``) so ``user.has_perm(...)`` calls flow
through the engine without hitting the now-empty
``Permission`` / ``Group`` M2Ms.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.contrib.auth import get_backends
from django.core.exceptions import PermissionDenied
from django.db import models


def _walk_backends(
    method_name: str, *args: Any, **kwargs: Any
) -> bool:
    """Iterate ``AUTHENTICATION_BACKENDS`` and stop on first ``True``.

    A backend may raise :class:`PermissionDenied` to short-circuit the
    chain — same contract as ``django.contrib.auth.models._user_has_*``.
    Backends that don't define the method are skipped silently.
    Note that ``rebac.errors.PermissionDenied`` subclasses Django's,
    so a REBAC-raised denial short-circuits the chain too.

    Other exceptions (``OperationalError``, custom backend bugs)
    propagate **by design** — also matches contrib.auth. Don't add a
    broad ``except`` here without changing
    ``django.contrib.auth.models._user_has_perm`` first; the chain
    semantics need to stay consistent.
    """
    for backend in get_backends():
        method = getattr(backend, method_name, None)
        if method is None:
            continue
        try:
            if method(*args, **kwargs):
                return True
        except PermissionDenied:
            return False
    return False


class RebacPermissionsMixin(models.Model):
    """Permission methods + ``is_superuser`` field, no M2Ms.

    Mirrors the public surface of
    :class:`django.contrib.auth.models.PermissionsMixin` minus the
    ``groups`` and ``user_permissions`` many-to-many relations. Pairs
    with :class:`rebac.backends.auth.RebacBackend` to route
    permission checks through the REBAC engine.

    Active superusers always pass; the ``REBAC_SUPERUSER_BYPASS``
    setting is honoured by the auth backend itself, so a project that
    flips the setting off can still keep this mixin — the engine
    simply receives the call and answers per relationship rows.
    """

    is_superuser = models.BooleanField(
        "superuser status",
        default=False,
        help_text=(
            "Designates that this user has all permissions without "
            "explicitly assigning them."
        ),
    )

    class Meta:
        abstract = True

    # ---------- has_perm / has_perms ----------

    def has_perm(self, perm: str, obj: Any = None) -> bool:
        """Return True if any backend grants ``perm`` (optionally on ``obj``).

        Active superusers short-circuit to True — matches contrib.auth
        so admin behaves identically for superuser sessions. Anyone
        else walks :setting:`AUTHENTICATION_BACKENDS`.
        """
        if self.is_active and self.is_superuser:
            return True
        return _walk_backends("has_perm", self, perm, obj)

    def has_perms(self, perm_list: Iterable[str], obj: Any = None) -> bool:
        """Return True iff every perm in ``perm_list`` is granted.

        ``perm_list`` must be iterable but not a bare string (matches
        contrib.auth's guard rail — a stringly-typed argument here is
        almost always a typo for ``has_perm``).
        """
        if not isinstance(perm_list, Iterable) or isinstance(
            perm_list, str
        ):
            raise ValueError("perm_list must be an iterable of permissions.")
        return all(self.has_perm(perm, obj) for perm in perm_list)

    # ---------- has_module_perms ----------

    def has_module_perms(self, app_label: str) -> bool:
        """Return True if any backend grants any permission on ``app_label``.

        Used by the admin index to decide whether to render an app's
        section. Superusers bypass.
        """
        if self.is_active and self.is_superuser:
            return True
        return _walk_backends("has_module_perms", self, app_label)


__all__ = ["RebacPermissionsMixin"]
