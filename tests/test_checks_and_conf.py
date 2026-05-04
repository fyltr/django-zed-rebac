"""Tests for system checks and settings cache behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.core import checks
from django.test import override_settings

from rebac.checks import check_cross_rbac_relations
from rebac.conf import app_settings


def test_app_settings_cache_invalidation_on_override_settings():
    assert app_settings.REBAC_BACKEND == "local"
    with override_settings(REBAC_BACKEND="spicedb"):
        assert app_settings.REBAC_BACKEND == "spicedb"
    assert app_settings.REBAC_BACKEND == "local"


def test_actor_middleware_must_be_after_authentication_middleware():
    with override_settings(
        MIDDLEWARE=[
            "rebac.middleware.ActorMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ]
    ):
        errors = checks.run_checks(tags=["rebac"])
    ids = {issue.id for issue in errors}
    assert "rebac.E004" in ids


def test_actor_middleware_requires_authentication_middleware_present():
    with override_settings(MIDDLEWARE=["rebac.middleware.ActorMiddleware"]):
        errors = checks.run_checks(tags=["rebac"])
    ids = {issue.id for issue in errors}
    assert "rebac.E003" in ids


# ---------------------------------------------------------------------------
# rebac.W003 — cross-RBAC FK/M2M warning
# ---------------------------------------------------------------------------


@override_settings(REBAC_LINT_BARE_PREFETCH=True)
def test_w003_fires_for_rbac_to_rbac_fk_in_testapp():
    """``Post.folder`` (RBAC) → ``Folder`` (RBAC) and ``Folder.parent``
    (self-FK) both fire W003 against the real testapp models."""
    issues = check_cross_rbac_relations()
    w003 = [i for i in issues if i.id == "rebac.W003"]
    messages = [i.msg for i in w003]
    # Post.folder → Folder (both RBAC-bound).
    assert any("testapp.Post.folder" in m and "testapp.Folder" in m for m in messages), messages
    # Folder.parent → Folder (self-FK, both RBAC-bound).
    assert any("testapp.Folder.parent" in m and "testapp.Folder" in m for m in messages), messages
    # Hint references the explicit Prefetch form.
    for issue in w003:
        assert issue.hint is not None
        assert "Prefetch(" in issue.hint
        assert ".with_actor(actor)" in issue.hint


def test_w003_off_by_default():
    """``REBAC_LINT_BARE_PREFETCH`` defaults to False — the check must
    early-return [] without walking the model graph."""
    assert check_cross_rbac_relations() == []


def _make_field(name, related_model, kind):
    """Build a duck-typed field instance whose ``isinstance`` check matches
    one of (FK, O2O, M2M) so the system check accepts it."""
    from django.db import models

    base = {"fk": models.ForeignKey, "o2o": models.OneToOneField, "m2m": models.ManyToManyField}[
        kind
    ]
    # Build a minimal subclass that bypasses Field.__init__ — we only need
    # isinstance() and the .name / .related_model attrs.
    instance = base.__new__(base)
    instance.name = name
    instance.related_model = related_model
    return instance


def _make_model(app_label, name, *, rebac_type, fields):
    """Build a duck-typed model surrogate with the attributes the check reads."""
    meta = SimpleNamespace(
        app_label=app_label,
        rebac_resource_type=rebac_type,
        get_fields=lambda: fields,
    )
    cls = type(name, (), {"_meta": meta, "__name__": name})
    return cls


@override_settings(REBAC_LINT_BARE_PREFETCH=True)
def test_w003_does_not_fire_for_fk_to_non_rbac_model():
    """An RBAC-bound model with an FK to a non-RBAC target (e.g. ``auth.User``)
    must NOT trip W003."""
    non_rbac = _make_model("auth", "User", rebac_type=None, fields=[])
    fk_field = _make_field("author", non_rbac, "fk")
    rbac_model = _make_model("blog", "Article", rebac_type="blog/article", fields=[fk_field])

    with patch("django.apps.apps.get_models", return_value=[rbac_model, non_rbac]):
        issues = check_cross_rbac_relations()

    w003 = [i for i in issues if i.id == "rebac.W003"]
    # No W003 against blog.Article.author should appear.
    assert not any("blog.Article.author" in i.msg for i in w003), [i.msg for i in w003]


@override_settings(REBAC_LINT_BARE_PREFETCH=True)
def test_w003_does_not_fire_for_non_rbac_model_pointing_at_rbac():
    """The warning is about RBAC→RBAC traversal only. A non-RBAC source
    pointing at an RBAC target must NOT trip W003."""
    rbac_target = _make_model("blog", "Article", rebac_type="blog/article", fields=[])
    fk_field = _make_field("article", rbac_target, "fk")
    non_rbac_source = _make_model("stats", "PageView", rebac_type=None, fields=[fk_field])

    with patch("django.apps.apps.get_models", return_value=[rbac_target, non_rbac_source]):
        issues = check_cross_rbac_relations()

    w003 = [i for i in issues if i.id == "rebac.W003"]
    # Nothing should be emitted from the non-RBAC source.
    assert not any("stats.PageView" in i.msg for i in w003), [i.msg for i in w003]
