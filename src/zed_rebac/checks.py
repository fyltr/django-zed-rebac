"""System checks — registered at app-ready. No DB queries; no model instantiation."""
from __future__ import annotations

from typing import Any

from django.core import checks

from .conf import app_settings


@checks.register("zed_rebac")
def check_backend_setting(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    issues: list[checks.CheckMessage] = []
    backend = app_settings.ZED_REBAC_BACKEND
    if backend not in ("local", "spicedb"):
        issues.append(
            checks.Error(
                f"ZED_REBAC_BACKEND={backend!r} (expected 'local' or 'spicedb')",
                id="zed_rebac.E001",
            )
        )
    if backend == "spicedb":
        if not app_settings.ZED_REBAC_SPICEDB_ENDPOINT:
            issues.append(
                checks.Error(
                    "ZED_REBAC_SPICEDB_ENDPOINT must be set when "
                    "ZED_REBAC_BACKEND='spicedb'",
                    id="zed_rebac.E002",
                )
            )
        if not app_settings.ZED_REBAC_SPICEDB_TOKEN:
            issues.append(
                checks.Error(
                    "ZED_REBAC_SPICEDB_TOKEN must be set when "
                    "ZED_REBAC_BACKEND='spicedb'",
                    id="zed_rebac.E002",
                )
            )
    return issues


@checks.register("zed_rebac", deploy=True)
def check_production_settings(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    issues: list[checks.CheckMessage] = []
    if (
        app_settings.ZED_REBAC_BACKEND == "spicedb"
        and not app_settings.ZED_REBAC_SPICEDB_TLS
    ):
        issues.append(
            checks.Warning(
                "ZED_REBAC_SPICEDB_TLS=False in production. Set to True for "
                "non-localhost SpiceDB endpoints.",
                id="zed_rebac.W101",
            )
        )
    return issues


@checks.register("zed_rebac")
def check_auth_backend_installed(
    app_configs: Any = None, **kwargs: Any
) -> list[checks.CheckMessage]:
    """Warn if `zed_rebac.backends.auth.ZedRBACBackend` is not in AUTHENTICATION_BACKENDS."""
    from django.conf import settings

    backends = getattr(settings, "AUTHENTICATION_BACKENDS", [])
    if not any(
        b.endswith(".ZedRBACBackend") or b.endswith(".auth.ZedRBACBackend")
        for b in backends
    ):
        return [
            checks.Warning(
                "zed_rebac.backends.auth.ZedRBACBackend not in AUTHENTICATION_BACKENDS. "
                "Per-object `user.has_perm(perm, obj)` checks will not route through REBAC.",
                id="zed_rebac.W001",
                hint=(
                    'Add "zed_rebac.backends.auth.ZedRBACBackend" to '
                    "AUTHENTICATION_BACKENDS, before django.contrib.auth.backends.ModelBackend."
                ),
            )
        ]
    return []
