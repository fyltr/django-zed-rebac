from __future__ import annotations

from django.apps import AppConfig


class TestappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tests.testapp"
    label = "testapp"
    rebac_schema = "permissions.zed"
