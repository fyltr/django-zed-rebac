"""System checks — registered at app-ready. No DB queries; no model instantiation."""
from __future__ import annotations

from typing import Any

from django.core import checks

from .conf import app_settings


@checks.register("rebac")
def check_backend_setting(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    issues: list[checks.CheckMessage] = []
    backend = app_settings.REBAC_BACKEND
    if backend not in ("local", "spicedb"):
        issues.append(
            checks.Error(
                f"REBAC_BACKEND={backend!r} (expected 'local' or 'spicedb')",
                id="rebac.E001",
            )
        )
    if backend == "spicedb":
        if not app_settings.REBAC_SPICEDB_ENDPOINT:
            issues.append(
                checks.Error(
                    "REBAC_SPICEDB_ENDPOINT must be set when "
                    "REBAC_BACKEND='spicedb'",
                    id="rebac.E002",
                )
            )
        if not app_settings.REBAC_SPICEDB_TOKEN:
            issues.append(
                checks.Error(
                    "REBAC_SPICEDB_TOKEN must be set when "
                    "REBAC_BACKEND='spicedb'",
                    id="rebac.E002",
                )
            )
    return issues


@checks.register("rebac", deploy=True)
def check_production_settings(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    issues: list[checks.CheckMessage] = []
    if (
        app_settings.REBAC_BACKEND == "spicedb"
        and not app_settings.REBAC_SPICEDB_TLS
    ):
        issues.append(
            checks.Warning(
                "REBAC_SPICEDB_TLS=False in production. Set to True for "
                "non-localhost SpiceDB endpoints.",
                id="rebac.W101",
            )
        )
    return issues


@checks.register("rebac")
def check_auth_backend_installed(
    app_configs: Any = None, **kwargs: Any
) -> list[checks.CheckMessage]:
    """Warn if `rebac.backends.auth.RebacBackend` is not in AUTHENTICATION_BACKENDS."""
    from django.conf import settings

    backends = getattr(settings, "AUTHENTICATION_BACKENDS", [])
    if not any(
        b.endswith(".RebacBackend") or b.endswith(".auth.RebacBackend")
        for b in backends
    ):
        return [
            checks.Warning(
                "rebac.backends.auth.RebacBackend not in AUTHENTICATION_BACKENDS. "
                "Per-object `user.has_perm(perm, obj)` checks will not route through REBAC.",
                id="rebac.W001",
                hint=(
                    'Add "rebac.backends.auth.RebacBackend" to '
                    "AUTHENTICATION_BACKENDS, before django.contrib.auth.backends.ModelBackend."
                ),
            )
        ]
    return []
