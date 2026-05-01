"""Relationship — Tier 3 core REBAC store.

Wire-shape mirrors `authzed.api.v1.Relationship` exactly. Renames are breaking.
"""
from __future__ import annotations

from django.db import models


class Relationship(models.Model):
    resource_type = models.CharField(max_length=64, db_index=True)
    resource_id = models.CharField(max_length=64, db_index=True)
    relation = models.CharField(max_length=64, db_index=True)
    subject_type = models.CharField(max_length=64, db_index=True)
    subject_id = models.CharField(max_length=64, db_index=True)
    optional_subject_relation = models.CharField(max_length=64, blank=True, default="")
    caveat_name = models.CharField(max_length=64, blank=True, default="")
    caveat_context = models.JSONField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    written_at_xid = models.BigIntegerField(default=0, db_index=True)

    class Meta:
        app_label = "rebac"
        verbose_name = "Relationship"
        verbose_name_plural = "Relationships"
        indexes = [
            # Forward: "what subjects have <relation> on <resource>?"
            models.Index(
                fields=["resource_type", "resource_id", "relation"],
                name="zr_rel_fwd_idx",
            ),
            # Reverse: "what resources does <subject> have <relation> on?"
            models.Index(
                fields=["subject_type", "subject_id", "relation"],
                name="zr_rel_rev_idx",
            ),
            # Subject-set traversal (group#member -> user)
            models.Index(
                fields=["subject_type", "subject_id", "optional_subject_relation"],
                name="zr_rel_subset_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "resource_type",
                    "resource_id",
                    "relation",
                    "subject_type",
                    "subject_id",
                    "optional_subject_relation",
                    "caveat_name",
                ],
                name="rebac_relationship_uniq",
            ),
        ]

    def __str__(self) -> str:
        rel = f"#{self.optional_subject_relation}" if self.optional_subject_relation else ""
        return (
            f"{self.resource_type}:{self.resource_id}#{self.relation} "
            f"@ {self.subject_type}:{self.subject_id}{rel}"
        )
