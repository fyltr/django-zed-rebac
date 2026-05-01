"""Tests for codename → action mapping."""
from __future__ import annotations

import pytest

from rebac.codenames import codename_to_action


@pytest.mark.parametrize(
    "perm,action",
    [
        ("blog.view_post", "read"),
        ("blog.change_post", "write"),
        ("blog.delete_post", "delete"),
        ("blog.add_post", "create"),
        ("view_post", "read"),
    ],
)
def test_default_codename_map(perm, action):
    assert codename_to_action(perm) == action


def test_unknown_returns_none():
    assert codename_to_action("blog.publish_post") is None
    assert codename_to_action("blog.foo") is None


def test_overrides_take_precedence():
    overrides = {"blog.publish_post": "publish"}
    assert codename_to_action("blog.publish_post", overrides=overrides) == "publish"
