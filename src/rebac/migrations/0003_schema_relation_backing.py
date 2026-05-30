from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("rebac", "0002_rebac_resource"),
    ]

    operations = [
        migrations.AddField(
            model_name="schemarelation",
            name="backing",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
