"""Settings access. All values prefixed `REBAC_`. No nested dicts.

Read via the public `app_settings` proxy — never call `getattr(settings, ...)`
directly inside the package; use `app_settings.<KEY>`.
"""
from __future__ import annotations

from typing import Any

from django.conf import settings
from django.test.signals import setting_changed


_DEFAULTS: dict[str, Any] = {
    "REBAC_BACKEND": "local",
    "REBAC_RELATIONSHIP_MODEL": "rebac.Relationship",
    "REBAC_SPICEDB_ENDPOINT": None,
    "REBAC_SPICEDB_TOKEN": None,
    "REBAC_SPICEDB_TLS": True,
    "REBAC_SPICEDB_AUTO_WRITE_SCHEMA": True,
    "REBAC_SCHEMA_DIR": None,  # resolves to BASE_DIR/zed-rebac at use site
    "REBAC_DEPTH_LIMIT": 8,
    "REBAC_DEFAULT_CONSISTENCY": "minimize_latency",
    "REBAC_CACHE_ALIAS": "default",
    "REBAC_LOOKUP_CACHE_TTL": 60,
    "REBAC_PK_IN_THRESHOLD": 10000,
    "REBAC_STRICT_MODE": True,
    "REBAC_REQUIRE_SUDO_REASON": True,
    "REBAC_ALLOW_SUDO": True,
    "REBAC_GC_INTERVAL_SECONDS": 300,
    "REBAC_ACTOR_RESOLVER": "rebac.actors.default_resolver",
    "REBAC_TYPE_PREFIX": "",
    "REBAC_SUPERUSER_BYPASS": True,
    "REBAC_SYNC_DJANGO_GROUPS": False,
    "REBAC_USER_TYPE": "auth/user",
    "REBAC_GROUP_TYPE": "auth/group",
}


class _AppSettings:
    """Lazy proxy. Reads from `django.conf.settings`, falls back to defaults.

    Cached values invalidate on `setting_changed` signal (test ergonomics).
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        setting_changed.connect(self._on_changed)

    def _on_changed(self, sender: Any, setting: str, value: Any, **kwargs: Any) -> None:
        if setting in _DEFAULTS:
            self._cache.pop(setting, None)

    def __getattr__(self, name: str) -> Any:
        if name not in _DEFAULTS:
            raise AttributeError(f"Unknown ZED_REBAC setting: {name!r}")
        if name in self._cache:
            return self._cache[name]
        value = getattr(settings, name, _DEFAULTS[name])
        self._cache[name] = value
        return value

    def reset(self) -> None:
        self._cache.clear()


app_settings = _AppSettings()
