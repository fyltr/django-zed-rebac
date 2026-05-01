"""Tier 1 baseline — schema rows loaded from each app's permissions.zed."""
from __future__ import annotations

from django.db import models


class SchemaDefinition(models.Model):
    resource_type = models.CharField(max_length=64, unique=True)

    class Meta:
        app_label = "zed_rebac"

    def __str__(self) -> str:
        return self.resource_type


class SchemaRelation(models.Model):
    definition = models.ForeignKey(
        SchemaDefinition, on_delete=models.CASCADE, related_name="relations"
    )
    name = models.CharField(max_length=64)
    # Array of `{"type": "...", "relation": "...", "wildcard": bool}`.
    allowed_subjects = models.JSONField(default=list)
    caveat = models.CharField(max_length=64, blank=True, default="")
    with_expiration = models.BooleanField(default=False)

    class Meta:
        app_label = "zed_rebac"
        unique_together = [("definition", "name")]

    def __str__(self) -> str:
        return f"{self.definition.resource_type}#{self.name}"


class SchemaPermission(models.Model):
    definition = models.ForeignKey(
        SchemaDefinition, on_delete=models.CASCADE, related_name="permissions"
    )
    name = models.CharField(max_length=64)
    expression = models.TextField()

    class Meta:
        app_label = "zed_rebac"
        unique_together = [("definition", "name")]

    def __str__(self) -> str:
        return f"{self.definition.resource_type}#{self.name}"


class SchemaCaveat(models.Model):
    name = models.CharField(max_length=64, unique=True)
    # `[{"name": "ip", "type": "ipaddress"}, ...]`.
    params = models.JSONField(default=list)
    expression = models.TextField()

    class Meta:
        app_label = "zed_rebac"

    def __str__(self) -> str:
        return self.name
