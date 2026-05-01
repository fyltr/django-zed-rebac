from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Relationship",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("resource_type", models.CharField(db_index=True, max_length=64)),
                ("resource_id", models.CharField(db_index=True, max_length=64)),
                ("relation", models.CharField(db_index=True, max_length=64)),
                ("subject_type", models.CharField(db_index=True, max_length=64)),
                ("subject_id", models.CharField(db_index=True, max_length=64)),
                ("optional_subject_relation", models.CharField(blank=True, default="", max_length=64)),
                ("caveat_name", models.CharField(blank=True, default="", max_length=64)),
                ("caveat_context", models.JSONField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("written_at_xid", models.BigIntegerField(db_index=True, default=0)),
            ],
            options={
                "verbose_name": "Relationship",
                "verbose_name_plural": "Relationships",
            },
        ),
        migrations.AddIndex(
            model_name="relationship",
            index=models.Index(
                fields=["resource_type", "resource_id", "relation"], name="zr_rel_fwd_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="relationship",
            index=models.Index(
                fields=["subject_type", "subject_id", "relation"], name="zr_rel_rev_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="relationship",
            index=models.Index(
                fields=["subject_type", "subject_id", "optional_subject_relation"],
                name="zr_rel_subset_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="relationship",
            constraint=models.UniqueConstraint(
                fields=(
                    "resource_type",
                    "resource_id",
                    "relation",
                    "subject_type",
                    "subject_id",
                    "optional_subject_relation",
                    "caveat_name",
                ),
                name="rebac_relationship_uniq",
            ),
        ),
        migrations.CreateModel(
            name="SchemaDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("resource_type", models.CharField(max_length=64, unique=True)),
            ],
        ),
        migrations.CreateModel(
            name="SchemaRelation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=64)),
                ("allowed_subjects", models.JSONField(default=list)),
                ("caveat", models.CharField(blank=True, default="", max_length=64)),
                ("with_expiration", models.BooleanField(default=False)),
                (
                    "definition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="relations",
                        to="rebac.schemadefinition",
                    ),
                ),
            ],
            options={"unique_together": {("definition", "name")}},
        ),
        migrations.CreateModel(
            name="SchemaPermission",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=64)),
                ("expression", models.TextField()),
                (
                    "definition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="permissions",
                        to="rebac.schemadefinition",
                    ),
                ),
            ],
            options={"unique_together": {("definition", "name")}},
        ),
        migrations.CreateModel(
            name="SchemaCaveat",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=64, unique=True)),
                ("params", models.JSONField(default=list)),
                ("expression", models.TextField()),
            ],
        ),
        migrations.CreateModel(
            name="PackageManagedRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("package", models.CharField(max_length=128)),
                ("external_id", models.CharField(max_length=255)),
                ("schema_revision", models.PositiveIntegerField()),
                ("target_pk", models.PositiveIntegerField()),
                ("content_hash", models.CharField(max_length=64)),
                ("no_update", models.BooleanField(default=True)),
                ("last_synced_at", models.DateTimeField()),
                (
                    "target_ct",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="contenttypes.contenttype",
                    ),
                ),
            ],
            options={
                "unique_together": {("package", "external_id")},
            },
        ),
        migrations.AddIndex(
            model_name="packagemanagedrecord",
            index=models.Index(
                fields=["target_ct", "target_pk"], name="zr_pmr_target_idx"
            ),
        ),
        migrations.CreateModel(
            name="SchemaOverride",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("tighten", "Tighten"),
                            ("loosen", "Loosen"),
                            ("disable", "Disable"),
                            ("extend", "Extend"),
                            ("recaveat", "Recaveat"),
                        ],
                        max_length=16,
                    ),
                ),
                ("target_pk", models.PositiveIntegerField()),
                ("expression", models.TextField()),
                ("reason", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "target_ct",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="contenttypes.contenttype",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="schemaoverride",
            index=models.Index(
                fields=["target_ct", "target_pk"], name="zr_ovr_target_idx"
            ),
        ),
        migrations.CreateModel(
            name="PermissionAuditEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("rel.grant", "Relationship grant"),
                            ("rel.revoke", "Relationship revoke"),
                            ("override.create", "Override create"),
                            ("override.delete", "Override delete"),
                            ("schema.sync", "Schema sync"),
                            ("sudo.bypass", "Sudo bypass"),
                        ],
                        max_length=32,
                    ),
                ),
                ("actor_subject_type", models.CharField(blank=True, default="", max_length=64)),
                ("actor_subject_id", models.CharField(blank=True, default="", max_length=64)),
                ("target_repr", models.CharField(blank=True, default="", max_length=512)),
                ("before", models.JSONField(blank=True, null=True)),
                ("after", models.JSONField(blank=True, null=True)),
                ("reason", models.TextField(blank=True, default="")),
                ("occurred_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={"ordering": ["-occurred_at"]},
        ),
    ]
