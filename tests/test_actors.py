"""Tests for actor resolution + sudo / system_context."""
from __future__ import annotations

import pytest

from zed_rebac import (
    NoActorResolvedError,
    SubjectRef,
    actor_context,
    current_actor,
    sudo,
    to_subject_ref,
    zed_subject,
)
from zed_rebac.actors import is_sudo, current_sudo_reason
from zed_rebac.errors import SudoReasonRequiredError


def test_subject_ref_passes_through():
    s = SubjectRef.of("auth/user", "42")
    assert to_subject_ref(s) is s


@pytest.mark.django_db
def test_django_user_to_subject_ref():
    from django.contrib.auth import get_user_model
    User = get_user_model()
    u = User.objects.create(username="alice", is_active=True)
    ref = to_subject_ref(u)
    assert ref.subject_type == "auth/user"
    assert ref.subject_id == str(u.pk)


@pytest.mark.django_db
def test_django_group_to_subject_ref():
    from django.contrib.auth.models import Group
    g = Group.objects.create(name="eng")
    ref = to_subject_ref(g)
    assert ref.subject_type == "auth/group"
    assert ref.optional_relation == "member"


def test_anonymous_or_unknown_raises():
    with pytest.raises(NoActorResolvedError):
        to_subject_ref(object())


def test_zed_subject_decorator_registers():
    @zed_subject(type="auth/apikey", id_attr="public_id")
    class ApiKey:
        def __init__(self, public_id: str) -> None:
            self.public_id = public_id

    ref = to_subject_ref(ApiKey("abc"))
    assert ref.subject_type == "auth/apikey"
    assert ref.subject_id == "abc"


def test_actor_context_block_scope():
    assert current_actor() is None
    s = SubjectRef.of("auth/user", "1")
    with actor_context(s):
        assert current_actor() == s
    assert current_actor() is None


def test_sudo_requires_reason_when_strict():
    with pytest.raises(SudoReasonRequiredError):
        with sudo():
            pass


def test_sudo_with_reason_flips_flag():
    assert not is_sudo()
    with sudo(reason="cron.test"):
        assert is_sudo()
        assert current_sudo_reason() == "cron.test"
    assert not is_sudo()
