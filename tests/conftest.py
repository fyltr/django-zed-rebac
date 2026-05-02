"""pytest-django configuration."""

from __future__ import annotations

import django
from django.conf import settings


def pytest_configure() -> None:
    if not settings.configured:
        from . import settings as test_settings  # noqa: F401
    django.setup()
