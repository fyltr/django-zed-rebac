"""SpiceDB-native .zed schema parser + AST + compiler."""

from __future__ import annotations

from .ast import (
    AllowedSubject,
    Caveat,
    CaveatParam,
    Definition,
    FieldBinding,
    PermArrow,
    PermBinOp,
    PermExpr,
    Permission,
    PermNil,
    PermRef,
    Relation,
    Schema,
)
from .parser import ParseError, parse_permission_expression, parse_zed, validate_schema

__all__ = [
    "AllowedSubject",
    "Caveat",
    "CaveatParam",
    "Definition",
    "FieldBinding",
    "ParseError",
    "PermArrow",
    "PermBinOp",
    "PermExpr",
    "PermNil",
    "PermRef",
    "Permission",
    "Relation",
    "Schema",
    "parse_permission_expression",
    "parse_permission_expression",
    "parse_zed",
    "validate_schema",
]
