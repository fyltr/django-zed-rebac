"""App configuration. Two lines in ready() per spec — no queries, no I/O."""
from __future__ import annotations

from django.apps import AppConfig


class ZedRebacConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "zed_rebac"
    verbose_name = "ZED-REBAC"
    label = "zed_rebac"
    default = True

    def ready(self) -> None:
        # Connect signal handlers + register system checks. No DB queries here.
        from . import checks  # noqa: F401  — side-effect: register checks
        from . import signals  # noqa: F401  — side-effect: connect handlers
