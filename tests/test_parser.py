"""Tests for the .zed schema parser."""

from __future__ import annotations

import pytest

from rebac.schema import (
    AllowedSubject,
    PermArrow,
    PermBinOp,
    PermNil,
    PermRef,
    parse_zed,
    validate_schema,
)
from rebac.schema.parser import ParseError

SAMPLE = """
// @rebac_package: blog
// @rebac_package_version: 0.1.0

use typechecking

definition auth/user {}
definition auth/group {
    relation member: auth/user | auth/group#member
}

caveat ip_in_cidr(ip ipaddress, cidr string) {
    ip.in_cidr(cidr)
}

definition blog/post {
    relation owner:  auth/user
    relation viewer: auth/user | auth/group#member | auth/user:*
    relation folder: blog/folder
    relation gated:  auth/user with ip_in_cidr

    permission read   = owner + viewer + folder->read
    permission write  = owner
    permission delete = owner & gated
    permission audit  = owner - viewer
}

definition blog/folder {
    relation owner: auth/user
    relation parent: blog/folder
    permission read = owner + parent->read
}
"""


def test_parses_full_schema():
    schema = parse_zed(SAMPLE)
    assert schema.headers["rebac_package"] == "blog"
    assert schema.headers["rebac_package_version"] == "0.1.0"
    assert "use typechecking" in schema.directives[0]
    assert {d.resource_type for d in schema.definitions} == {
        "auth/user",
        "auth/group",
        "blog/post",
        "blog/folder",
    }
    cav = schema.get_caveat("ip_in_cidr")
    assert cav is not None
    assert [(p.name, p.type) for p in cav.params] == [
        ("ip", "ipaddress"),
        ("cidr", "string"),
    ]

    post = schema.get_definition("blog/post")
    assert post is not None
    viewer = next(r for r in post.relations if r.name == "viewer")
    assert AllowedSubject("auth/user") in viewer.allowed_subjects
    assert AllowedSubject("auth/group", "member") in viewer.allowed_subjects
    assert any(s.wildcard for s in viewer.allowed_subjects)

    gated = next(r for r in post.relations if r.name == "gated")
    assert gated.allowed_subjects[0].with_caveat == "ip_in_cidr"


def test_permission_expression_precedence():
    # Per SpiceDB: + binds tightest, & next, - loosest.
    # So  "a + b & c"  parses as  (a + b) & c.
    schema = parse_zed(
        """
        definition x/y {
            relation a: auth/user
            relation b: auth/user
            relation c: auth/user
            permission p = a + b & c
        }
        """
    )
    perm = schema.get_permission("x/y", "p")
    assert perm is not None
    expr = perm.expression
    assert isinstance(expr, PermBinOp)
    assert expr.op == "&"
    assert isinstance(expr.left, PermBinOp)
    assert expr.left.op == "+"
    assert isinstance(expr.right, PermRef)
    assert expr.right.name == "c"


def test_permission_with_arrow():
    schema = parse_zed(
        """
        definition x/y {
            relation parent: x/y
            permission read = parent->read
        }
        """
    )
    perm = schema.get_permission("x/y", "read")
    assert perm is not None
    assert isinstance(perm.expression, PermArrow)
    assert perm.expression.via == "parent"
    assert perm.expression.target == "read"


def test_permission_with_parentheses_overrides_precedence():
    schema = parse_zed(
        """
        definition x/y {
            relation a: auth/user
            relation b: auth/user
            relation c: auth/user
            permission p = a + (b & c)
        }
        """
    )
    perm = schema.get_permission("x/y", "p")
    assert isinstance(perm.expression, PermBinOp)
    assert perm.expression.op == "+"
    assert isinstance(perm.expression.right, PermBinOp)
    assert perm.expression.right.op == "&"


def test_nil_permission():
    schema = parse_zed(
        """
        definition x/y {
            permission unreachable = nil
        }
        """
    )
    perm = schema.get_permission("x/y", "unreachable")
    assert isinstance(perm.expression, PermNil)


def test_validate_schema_detects_undefined_reference():
    schema = parse_zed(
        """
        definition x/y {
            permission p = nonexistent
        }
        """
    )
    errors = validate_schema(schema)
    assert any("undefined reference" in e for e in errors)


def test_unterminated_block_raises():
    with pytest.raises(ParseError):
        parse_zed("definition x/y { relation a: auth/user")
