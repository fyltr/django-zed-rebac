"""Django models.

Six tables: Relationship + Schema* (4) + PackageManagedRecord + SchemaOverride
+ PermissionAuditEvent. Per ARCHITECTURE.md § Models.
"""
from __future__ import annotations

from .audit import PermissionAuditEvent
from .overrides import SchemaOverride
from .provenance import PackageManagedRecord
from .relationship import Relationship
from .schema import (
    SchemaCaveat,
    SchemaDefinition,
    SchemaPermission,
    SchemaRelation,
)

__all__ = [
    "Relationship",
    "SchemaDefinition",
    "SchemaRelation",
    "SchemaPermission",
    "SchemaCaveat",
    "PackageManagedRecord",
    "SchemaOverride",
    "PermissionAuditEvent",
]
