"""SpiceDB-native .zed schema parser + AST + compiler."""
from __future__ import annotations

from .ast import (
    AllowedSubject,
    Caveat,
    CaveatParam,
    Definition,
    Permission,
    PermExpr,
    PermArrow,
    PermBinOp,
    PermNil,
    PermRef,
    Relation,
    Schema,
)
from .parser import ParseError, parse_zed, validate_schema

__all__ = [
    "Schema",
    "Definition",
    "Relation",
    "AllowedSubject",
    "Permission",
    "PermExpr",
    "PermBinOp",
    "PermArrow",
    "PermRef",
    "PermNil",
    "Caveat",
    "CaveatParam",
    "parse_zed",
    "validate_schema",
    "ParseError",
]
