"""SluggedPost — adds a model that uses a non-pk REBAC id_attr."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("testapp", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SluggedPost",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("slug", models.CharField(max_length=64, unique=True)),
                ("title", models.CharField(max_length=200)),
            ],
        ),
    ]
