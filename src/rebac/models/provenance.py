"""PackageManagedRecord — Tier 1 provenance.

Borrowed from Odoo 18's `ir.model.data`. Tracks which package shipped which
schema row with `noupdate` semantics that preserve admin edits across upgrades.
"""

from __future__ import annotations

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class PackageManagedRecord(models.Model):
    package = models.CharField(max_length=128)
    external_id = models.CharField(max_length=255)
    schema_revision = models.PositiveIntegerField()
    target_ct = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    target_pk = models.PositiveIntegerField()
    target = GenericForeignKey("target_ct", "target_pk")
    content_hash = models.CharField(max_length=64)
    no_update = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField()

    class Meta:
        app_label = "rebac"
        unique_together = [("package", "external_id")]
        indexes = [models.Index(fields=["target_ct", "target_pk"])]

    def __str__(self) -> str:
        return f"{self.package}:{self.external_id}"
