"""Tests for system checks and settings cache behavior."""

from __future__ import annotations

from django.core import checks
from django.test import override_settings

from rebac.conf import app_settings


def test_app_settings_cache_invalidation_on_override_settings():
    assert app_settings.REBAC_BACKEND == "local"
    with override_settings(REBAC_BACKEND="spicedb"):
        assert app_settings.REBAC_BACKEND == "spicedb"
    assert app_settings.REBAC_BACKEND == "local"


def test_actor_middleware_must_be_after_authentication_middleware():
    with override_settings(
        MIDDLEWARE=[
            "rebac.middleware.ActorMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ]
    ):
        errors = checks.run_checks(tags=["rebac"])
    ids = {issue.id for issue in errors}
    assert "rebac.E004" in ids


def test_actor_middleware_requires_authentication_middleware_present():
    with override_settings(MIDDLEWARE=["rebac.middleware.ActorMiddleware"]):
        errors = checks.run_checks(tags=["rebac"])
    ids = {issue.id for issue in errors}
    assert "rebac.E003" in ids
