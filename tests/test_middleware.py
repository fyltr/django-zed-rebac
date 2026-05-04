"""Tests for ``rebac.middleware.ActorMiddleware`` — superuser bypass.

The middleware opens a ``sudo(reason="superuser-bypass")`` bracket for
the request lifetime when the user is an active superuser AND both
``REBAC_SUPERUSER_BYPASS`` and ``REBAC_ALLOW_SUDO`` are True.

These tests pin three things the bypass must guarantee:

  - It activates only for active superusers under both feature flags.
  - It routes through the public ``sudo()`` API so every elevated
    request emits a ``KIND_SUDO_BYPASS`` audit row (CLAUDE.md § 3).
  - Both the actor ContextVar and the sudo bracket are torn down in
    LIFO order, including when ``get_response`` raises.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from rebac.actors import current_actor, is_sudo
from rebac.middleware import ActorMiddleware
from rebac.models import PermissionAuditEvent


class _FakeRequest:
    def __init__(self, user):
        self.user = user


@pytest.fixture
def superuser(db):
    User = get_user_model()
    return User.objects.create_superuser(username="root", email="root@example.com", password="x")


@pytest.fixture
def regular_user(db):
    User = get_user_model()
    return User.objects.create_user(username="alice", email="alice@example.com", password="x")


def _capture(captured):
    """Build a get_response that records sudo state at request time."""

    def get_response(request):
        captured["is_sudo"] = is_sudo()
        captured["actor"] = current_actor()
        return "ok"

    return get_response


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=True)
def test_superuser_request_runs_inside_sudo_bracket(superuser):
    PermissionAuditEvent.objects.all().delete()
    captured: dict = {}
    mw = ActorMiddleware(_capture(captured))

    response = mw(_FakeRequest(superuser))

    assert response == "ok"
    assert captured["is_sudo"] is True
    # Actor ContextVar was set from the resolver too.
    assert captured["actor"] is not None
    # Bracket teardown ran — no leak past the request.
    assert is_sudo() is False
    assert current_actor() is None
    # Audit row was emitted with the bypass reason.
    rows = list(PermissionAuditEvent.objects.filter(kind="sudo.bypass", reason="superuser-bypass"))
    assert len(rows) == 1


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=True)
def test_regular_user_request_does_not_open_sudo(regular_user):
    PermissionAuditEvent.objects.all().delete()
    captured: dict = {}
    mw = ActorMiddleware(_capture(captured))

    mw(_FakeRequest(regular_user))

    assert captured["is_sudo"] is False
    assert PermissionAuditEvent.objects.filter(reason="superuser-bypass").count() == 0


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=True)
def test_inactive_superuser_does_not_open_sudo(db):
    User = get_user_model()
    inactive = User.objects.create_superuser(
        username="ghost", email="ghost@example.com", password="x"
    )
    inactive.is_active = False
    inactive.save(update_fields=["is_active"])

    captured: dict = {}
    mw = ActorMiddleware(_capture(captured))
    mw(_FakeRequest(inactive))

    assert captured["is_sudo"] is False


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=False)
def test_bypass_disabled_setting_suppresses_sudo(superuser):
    captured: dict = {}
    mw = ActorMiddleware(_capture(captured))
    mw(_FakeRequest(superuser))
    assert captured["is_sudo"] is False


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=True, REBAC_ALLOW_SUDO=False)
def test_allow_sudo_false_suppresses_bypass(superuser):
    """Tenants that globally disable sudo must not get an implicit
    superuser bypass — the safe fail-closed answer."""
    captured: dict = {}
    mw = ActorMiddleware(_capture(captured))
    mw(_FakeRequest(superuser))
    assert captured["is_sudo"] is False


@pytest.mark.django_db
@override_settings(REBAC_SUPERUSER_BYPASS=True)
def test_exception_in_view_still_resets_actor_and_sudo(superuser):
    def boom(request):
        assert is_sudo() is True
        raise RuntimeError("view exploded")

    mw = ActorMiddleware(boom)
    with pytest.raises(RuntimeError, match="view exploded"):
        mw(_FakeRequest(superuser))

    # Both ContextVars must be torn down even on exception.
    assert is_sudo() is False
    assert current_actor() is None
