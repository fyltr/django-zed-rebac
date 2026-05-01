"""Test settings for pytest-django."""
from __future__ import annotations

SECRET_KEY = "test-secret-key-not-for-production"
DEBUG = True

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "zed_rebac",
    "tests.testapp",
]

AUTHENTICATION_BACKENDS = [
    "zed_rebac.backends.auth.ZedRBACBackend",
    "django.contrib.auth.backends.ModelBackend",
]

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Make tests deterministic — strict mode ON.
ZED_REBAC_BACKEND = "local"
ZED_REBAC_STRICT_MODE = True
ZED_REBAC_REQUIRE_SUDO_REASON = True
ZED_REBAC_ALLOW_SUDO = True
ZED_REBAC_SUPERUSER_BYPASS = False
