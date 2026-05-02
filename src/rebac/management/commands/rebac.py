"""Single management command with subcommands.

python manage.py rebac sync                     # idempotent
python manage.py rebac sync --check             # CI gate; non-zero on drift
python manage.py rebac sync --force-overwrite   # destructive
python manage.py rebac check                    # validate without writes
python manage.py rebac build-zed                # emit effective.zed
python manage.py rebac explain <type>.<perm>    # print compiled expression
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ...schema.parser import parse_zed, validate_schema


class Command(BaseCommand):
    help = "Manage rebac schema (sync / check / build-zed / explain)."

    def add_arguments(self, parser: Any) -> None:
        sub = parser.add_subparsers(dest="cmd", required=True)

        p_sync = sub.add_parser("sync", help="Load permissions.zed files into Schema* tables")
        p_sync.add_argument(
            "--check", action="store_true", help="CI gate: detect drift; no writes."
        )
        p_sync.add_argument(
            "--force-overwrite",
            action="store_true",
            help="Bypass no_update; destructive.",
        )
        p_sync.add_argument("--package", help="Limit to one package")
        p_sync.add_argument("--yes", action="store_true", help="Skip confirmation")

        sub.add_parser("check", help="Validate without writing.")

        p_build = sub.add_parser("build-zed", help="Emit unified effective.zed")
        p_build.add_argument("--out", default=None)

        p_explain = sub.add_parser("explain", help="Print compiled expression for <type>.<perm>")
        p_explain.add_argument("target", help="e.g. blog/post.read")

    def handle(self, *args: Any, **options: Any) -> None:
        cmd = options["cmd"]
        if cmd == "sync":
            self._handle_sync(options)
        elif cmd == "check":
            self._handle_check()
        elif cmd == "build-zed":
            self._handle_build_zed(options)
        elif cmd == "explain":
            self._handle_explain(options)
        else:
            raise CommandError(f"Unknown subcommand: {cmd}")

    # ---------- sync ----------

    def _handle_sync(self, options: dict[str, Any]) -> None:
        from django.db import transaction

        from ...models import (
            SchemaCaveat,
            SchemaDefinition,
            SchemaPermission,
            SchemaRelation,
        )

        check_only = options["check"]
        force = options["force_overwrite"]
        only_package = options.get("package")

        any_drift = False
        for app_config in apps.get_app_configs():
            schema_path = self._resolve_schema_path(app_config)
            if schema_path is None:
                continue
            package_name = app_config.name
            if only_package and only_package != package_name:
                continue

            text = schema_path.read_text(encoding="utf-8")
            schema = parse_zed(text)
            errors = validate_schema(schema)
            if errors:
                for e in errors:
                    self.stderr.write(self.style.ERROR(f"  {package_name}: {e}"))
                raise CommandError(f"Schema validation failed for {package_name}")

            self.stdout.write(f"-> {package_name} ({schema_path})")

            with transaction.atomic():
                # Caveats first (definitions reference them).
                for caveat in schema.caveats:
                    payload = {
                        "params": [{"name": p.name, "type": p.type} for p in caveat.params],
                        "expression": caveat.expression,
                    }
                    drift = self._sync_row(
                        SchemaCaveat,
                        natural_key={"name": caveat.name},
                        payload=payload,
                        package=package_name,
                        external_id=f"caveat:{caveat.name}",
                        check_only=check_only,
                        force=force,
                    )
                    any_drift = any_drift or drift

                # Definitions
                for d in schema.definitions:
                    drift = self._sync_row(
                        SchemaDefinition,
                        natural_key={"resource_type": d.resource_type},
                        payload={},
                        package=package_name,
                        external_id=f"definition:{d.resource_type}",
                        check_only=check_only,
                        force=force,
                    )
                    any_drift = any_drift or drift
                    if check_only:
                        continue
                    schema_def = SchemaDefinition.objects.get(resource_type=d.resource_type)

                    for r in d.relations:
                        allowed = [
                            {
                                "type": s.type,
                                "relation": s.relation,
                                "wildcard": s.wildcard,
                                "with_caveat": s.with_caveat,
                            }
                            for s in r.allowed_subjects
                        ]
                        SchemaRelation.objects.update_or_create(
                            definition=schema_def,
                            name=r.name,
                            defaults={
                                "allowed_subjects": allowed,
                                "caveat": "",
                                "with_expiration": r.with_expiration,
                            },
                        )

                    for p in d.permissions:
                        SchemaPermission.objects.update_or_create(
                            definition=schema_def,
                            name=p.name,
                            defaults={"expression": p.raw_text or "<expr>"},
                        )

        if check_only:
            if any_drift:
                raise CommandError("Schema drift detected. Run `rebac sync` to apply.")
            self.stdout.write(self.style.SUCCESS("OK — no drift."))
            return

        # Reset cached backend so the next access reloads the schema from DB.
        from ...backends import reset_backend

        reset_backend()
        self.stdout.write(self.style.SUCCESS("Sync complete."))

    def _resolve_schema_path(self, app_config: Any) -> Path | None:
        # Two ways to declare: `AppConfig.rebac_schema = "permissions.zed"` (rel
        # path), or a `permissions.zed` adjacent to apps.py.
        rel = getattr(app_config, "rebac_schema", None)
        if rel is not None:
            path = Path(app_config.path) / rel
        else:
            path = Path(app_config.path) / "permissions.zed"
        return path if path.exists() else None

    def _sync_row(
        self,
        model_cls: type,
        natural_key: dict[str, Any],
        payload: dict[str, Any],
        package: str,
        external_id: str,
        check_only: bool,
        force: bool,
    ) -> bool:
        """Sync one schema row, respecting `no_update`. Returns True if drifted."""
        from django.contrib.contenttypes.models import ContentType

        from ...models import PackageManagedRecord

        content_hash = self._hash_payload({**natural_key, **payload})
        existing = model_cls.objects.filter(**natural_key).first()
        ct = ContentType.objects.get_for_model(model_cls)

        record = PackageManagedRecord.objects.filter(
            package=package, external_id=external_id
        ).first()

        if existing is None:
            # Fresh.
            if check_only:
                return True
            obj = model_cls.objects.create(**natural_key, **payload)
            PackageManagedRecord.objects.update_or_create(
                package=package,
                external_id=external_id,
                defaults={
                    "schema_revision": 1,
                    "target_ct": ct,
                    "target_pk": obj.pk,
                    "content_hash": content_hash,
                    "no_update": True,
                    "last_synced_at": timezone.now(),
                },
            )
            return False

        # Already present. Decide based on hash + no_update.
        if record is None:
            # Orphan adoption — claim ownership.
            if check_only:
                return True
            for k, v in payload.items():
                setattr(existing, k, v)
            existing.save()
            PackageManagedRecord.objects.create(
                package=package,
                external_id=external_id,
                schema_revision=1,
                target_ct=ct,
                target_pk=existing.pk,
                content_hash=content_hash,
                no_update=True,
                last_synced_at=timezone.now(),
            )
            return False

        if record.content_hash == content_hash:
            return False  # no-op

        if record.no_update and not force:
            self.stdout.write(
                self.style.WARNING(
                    f"  ! drift: {external_id} (admin-edited; --force-overwrite to apply)"
                )
            )
            return True

        if check_only:
            return True
        for k, v in payload.items():
            setattr(existing, k, v)
        existing.save()
        record.content_hash = content_hash
        record.last_synced_at = timezone.now()
        record.save(update_fields=["content_hash", "last_synced_at"])
        return False

    @staticmethod
    def _hash_payload(payload: dict[str, Any]) -> str:
        import json

        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ---------- check ----------

    def _handle_check(self) -> None:
        any_errors = False
        for app_config in apps.get_app_configs():
            path = self._resolve_schema_path(app_config)
            if path is None:
                continue
            text = path.read_text(encoding="utf-8")
            try:
                schema = parse_zed(text)
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"{app_config.name}: {exc}"))
                any_errors = True
                continue
            errors = validate_schema(schema)
            for e in errors:
                self.stderr.write(self.style.ERROR(f"  {app_config.name}: {e}"))
            if errors:
                any_errors = True
        if any_errors:
            raise CommandError("Schema check failed.")
        self.stdout.write(self.style.SUCCESS("OK"))

    # ---------- build-zed ----------

    def _handle_build_zed(self, options: dict[str, Any]) -> None:
        out_path = options.get("out")
        # Concatenate every package's permissions.zed in alphabetical order.
        from ...conf import app_settings as conf_app_settings

        if out_path is None:
            base_dir = conf_app_settings.REBAC_SCHEMA_DIR or Path.cwd() / "rebac"
            base_dir = Path(base_dir)
            base_dir.mkdir(parents=True, exist_ok=True)
            out_path = base_dir / "effective.zed"
        else:
            out_path = Path(out_path)

        chunks: list[str] = [
            "// Generated by django-zed-rebac. Do not edit.\n",
            "use typechecking\n",
        ]
        for app_config in sorted(apps.get_app_configs(), key=lambda a: a.name):
            path = self._resolve_schema_path(app_config)
            if path is None:
                continue
            chunks.append(f"\n// from {app_config.name} ({path.name})\n")
            chunks.append(path.read_text(encoding="utf-8"))
        out_path.write_text("".join(chunks), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote {out_path}"))

    # ---------- explain ----------

    def _handle_explain(self, options: dict[str, Any]) -> None:
        target = options["target"]
        if "." not in target:
            raise CommandError("explain target must be <type>.<perm>")
        rt, perm = target.rsplit(".", 1)
        from ...models import SchemaDefinition

        try:
            d = SchemaDefinition.objects.get(resource_type=rt)
        except SchemaDefinition.DoesNotExist as exc:
            raise CommandError(f"No definition: {rt}") from exc
        try:
            p = d.permissions.get(name=perm)
        except Exception as exc:
            raise CommandError(f"No permission {perm!r} on {rt}: {exc}") from exc
        self.stdout.write(f"{rt}#{perm} = {p.expression}")
