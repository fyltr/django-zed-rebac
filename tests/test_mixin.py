"""End-to-end tests of ZedRBACMixin scoping."""
from __future__ import annotations

import pytest

from zed_rebac import (
    MissingActorError,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    write_relationships,
)
from zed_rebac.actors import _current_actor
from zed_rebac.backends import reset_backend
from zed_rebac.schema import parse_zed


SCHEMA_TEXT = """
definition auth/user {}
definition auth/group {
    relation member: auth/user
}
definition blog/folder {
    relation owner: auth/user
    relation parent: blog/folder
    permission read = owner + parent->read
    permission write = owner + parent->write
}
definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user | auth/group#member | auth/user:*
    relation folder: blog/folder
    permission read = owner + viewer + folder->read
    permission write = owner
    permission delete = owner
    permission create = owner
}
"""


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))  # type: ignore[attr-defined]
    yield
    reset_backend()


@pytest.fixture
def alice(db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create(username="alice", is_active=True)


@pytest.fixture
def bob(db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create(username="bob", is_active=True)


@pytest.fixture
def post(db):
    from tests.testapp.models import Post
    with sudo(reason="test.fixture"):
        p = Post.objects.create(title="hello")
    return p


def _grant_owner(user, post):
    write_relationships([
        RelationshipTuple(
            resource=ObjectRef("blog/post", str(post.pk)),
            relation="owner",
            subject=SubjectRef.of("auth/user", str(user.pk)),
        ),
    ])


def test_strict_mode_raises_without_actor(post):
    from tests.testapp.models import Post
    with pytest.raises(MissingActorError):
        list(Post.objects.all())


def test_as_user_scopes_to_owner(alice, bob, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post
    assert list(Post.objects.as_user(alice).values_list("pk", flat=True)) == [post.pk]
    assert list(Post.objects.as_user(bob)) == []


def test_with_actor_accepts_subject_ref(alice, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post
    ref = SubjectRef.of("auth/user", str(alice.pk))
    assert list(Post.objects.with_actor(ref).values_list("pk", flat=True)) == [post.pk]


def test_save_blocked_for_non_owner(alice, bob, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post
    # Bob loads the post via sudo, then tries to save under his own actor.
    with sudo(reason="test.load"):
        instance = Post.objects.get(pk=post.pk)
    instance._zed_actor = SubjectRef.of("auth/user", str(bob.pk))
    instance.title = "hijacked"
    with pytest.raises(PermissionDenied):
        instance.save()


def test_save_allowed_for_owner(alice, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post
    instance = Post.objects.as_user(alice).get(pk=post.pk)
    assert instance._zed_actor is not None
    instance.title = "renamed"
    instance.save()


def test_sudo_bypass_allows_create(alice):
    from tests.testapp.models import Post
    with sudo(reason="test.sudo"):
        p = Post.objects.create(title="created in sudo")
        assert p.pk is not None


def test_count_respects_scope(alice, bob, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post
    assert Post.objects.as_user(alice).count() == 1
    assert Post.objects.as_user(bob).count() == 0


def test_sudo_queryset_returns_all(alice, bob, post):
    _grant_owner(alice, post)
    from tests.testapp.models import Post
    qs = Post.objects.sudo(reason="test.sudo")
    assert qs.count() == 1
