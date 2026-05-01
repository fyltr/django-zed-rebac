from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True
    dependencies: list[tuple[str, str]] = []

    operations = [
        migrations.CreateModel(
            name="Folder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=100)),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="children",
                        to="testapp.folder",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Post",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=200)),
                ("body", models.TextField(blank=True, default="")),
                (
                    "folder",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="posts",
                        to="testapp.folder",
                    ),
                ),
            ],
        ),
    ]
