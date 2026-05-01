"""PermissionAuditEvent — append-only audit log."""
from __future__ import annotations

from django.db import models


class PermissionAuditEvent(models.Model):
    KIND_RELATIONSHIP_GRANT = "rel.grant"
    KIND_RELATIONSHIP_REVOKE = "rel.revoke"
    KIND_OVERRIDE_CREATE = "override.create"
    KIND_OVERRIDE_DELETE = "override.delete"
    KIND_SCHEMA_SYNC = "schema.sync"
    KIND_SUDO_BYPASS = "sudo.bypass"

    KIND_CHOICES = [
        (KIND_RELATIONSHIP_GRANT, "Relationship grant"),
        (KIND_RELATIONSHIP_REVOKE, "Relationship revoke"),
        (KIND_OVERRIDE_CREATE, "Override create"),
        (KIND_OVERRIDE_DELETE, "Override delete"),
        (KIND_SCHEMA_SYNC, "Schema sync"),
        (KIND_SUDO_BYPASS, "Sudo bypass"),
    ]

    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    actor_subject_type = models.CharField(max_length=64, blank=True, default="")
    actor_subject_id = models.CharField(max_length=64, blank=True, default="")
    target_repr = models.CharField(max_length=512, blank=True, default="")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        app_label = "zed_rebac"
        ordering = ["-occurred_at"]

    def __str__(self) -> str:
        actor = f"{self.actor_subject_type}:{self.actor_subject_id}" if self.actor_subject_type else "<system>"
        return f"[{self.kind}] {actor} → {self.target_repr}"
