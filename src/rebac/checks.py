"""System checks — registered at app-ready. No DB queries; no model instantiation."""

from __future__ import annotations

from typing import Any

from django.core import checks

from .conf import app_settings


@checks.register("rebac")
def check_backend_setting(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    issues: list[checks.CheckMessage] = []
    backend = app_settings.REBAC_BACKEND
    if backend not in ("local", "spicedb"):
        issues.append(
            checks.Error(
                f"REBAC_BACKEND={backend!r} (expected 'local' or 'spicedb')",
                id="rebac.E001",
            )
        )
    if backend == "spicedb":
        if not app_settings.REBAC_SPICEDB_ENDPOINT:
            issues.append(
                checks.Error(
                    "REBAC_SPICEDB_ENDPOINT must be set when REBAC_BACKEND='spicedb'",
                    id="rebac.E002",
                )
            )
        if not app_settings.REBAC_SPICEDB_TOKEN:
            issues.append(
                checks.Error(
                    "REBAC_SPICEDB_TOKEN must be set when REBAC_BACKEND='spicedb'",
                    id="rebac.E002",
                )
            )
    return issues


@checks.register("rebac", deploy=True)
def check_production_settings(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    issues: list[checks.CheckMessage] = []
    if app_settings.REBAC_BACKEND == "spicedb" and not app_settings.REBAC_SPICEDB_TLS:
        issues.append(
            checks.Warning(
                "REBAC_SPICEDB_TLS=False in production. Set to True for "
                "non-localhost SpiceDB endpoints.",
                id="rebac.W101",
            )
        )
    return issues


@checks.register("rebac")
def check_auth_backend_installed(
    app_configs: Any = None, **kwargs: Any
) -> list[checks.CheckMessage]:
    """Warn if `rebac.backends.auth.RebacBackend` is not in AUTHENTICATION_BACKENDS."""
    from django.conf import settings

    backends = getattr(settings, "AUTHENTICATION_BACKENDS", [])
    if not any(b.endswith(".RebacBackend") or b.endswith(".auth.RebacBackend") for b in backends):
        return [
            checks.Warning(
                "rebac.backends.auth.RebacBackend not in AUTHENTICATION_BACKENDS. "
                "Per-object `user.has_perm(perm, obj)` checks will not route through REBAC.",
                id="rebac.W001",
                hint=(
                    'Add "rebac.backends.auth.RebacBackend" to '
                    "AUTHENTICATION_BACKENDS, before django.contrib.auth.backends.ModelBackend."
                ),
            )
        ]
    return []


@checks.register("rebac")
def check_actor_middleware_order(
    app_configs: Any = None, **kwargs: Any
) -> list[checks.CheckMessage]:
    """Ensure ActorMiddleware appears after AuthenticationMiddleware."""
    from django.conf import settings

    middleware = list(getattr(settings, "MIDDLEWARE", []))
    actor_path = "rebac.middleware.ActorMiddleware"
    auth_path = "django.contrib.auth.middleware.AuthenticationMiddleware"
    if actor_path not in middleware:
        return []
    if auth_path not in middleware:
        return [
            checks.Error(
                f"{actor_path} requires {auth_path} in MIDDLEWARE.",
                id="rebac.E003",
            )
        ]
    if middleware.index(actor_path) < middleware.index(auth_path):
        return [
            checks.Error(
                f"{actor_path} must appear after {auth_path} in MIDDLEWARE.",
                id="rebac.E004",
            )
        ]
    return []


def _is_rebac_bound(model: Any) -> bool:
    """A model is RBAC-bound iff its ``_meta`` carries a truthy
    ``rebac_resource_type`` attribute (set by ``RebacModelBase`` from
    ``Meta.rebac_resource_type``)."""
    meta = getattr(model, "_meta", None)
    if meta is None:
        return False
    return bool(getattr(meta, "rebac_resource_type", None))


@checks.register("rebac")
def check_cross_rbac_relations(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    """Warn when an RBAC-bound model declares a forward FK / O2O / M2M whose
    target is also RBAC-bound.

    Bare-string ``qs.prefetch_related("rel")`` against an RBAC-bound related
    model does not auto-scope in v1 — callers must use the explicit
    ``Prefetch(queryset=Related.objects.with_actor(actor))`` form. This check
    surfaces every such relation at startup so the JOIN-leak surface is
    greppable. Reverse accessors are out of scope for v1.

    Off by default because the warning fires for the *existence* of the
    relation, not for any actual bare-string usage — on a healthy
    codebase that already wraps prefetches in
    ``Prefetch(queryset=...with_actor(actor))`` the check is pure noise
    and drowns real warnings. Opt in by setting
    ``REBAC_LINT_BARE_PREFETCH = True`` when you want the one-off audit.
    """
    if not app_settings.REBAC_LINT_BARE_PREFETCH:
        return []

    from django.apps import apps
    from django.db import models

    issues: list[checks.CheckMessage] = []
    relation_types = (models.ForeignKey, models.OneToOneField, models.ManyToManyField)
    for model in apps.get_models():
        if not _is_rebac_bound(model):
            continue
        for field in model._meta.get_fields():
            # Limit to forward declarations: skip reverse accessors and
            # auto-created intermediates.
            if not isinstance(field, relation_types):
                continue
            related = getattr(field, "related_model", None)
            if related is None:
                continue
            if not _is_rebac_bound(related):
                continue
            model_label = f"{model._meta.app_label}.{model.__name__}"
            related_label = f"{related._meta.app_label}.{related.__name__}"
            issues.append(
                checks.Warning(
                    f"{model_label}.{field.name}: bare-string "
                    "select_related/prefetch_related against RBAC-bound "
                    f"related model {related_label} won't auto-scope.",
                    hint=(
                        f'Use Prefetch("{field.name}", queryset='
                        f"{related.__name__}.objects.with_actor(actor))."
                    ),
                    obj=model,
                    id="rebac.W003",
                )
            )
    return issues
