"""Core public types: subject/resource references, check results, Zookie.

Wire-shape mirrors `authzed.api.v1` so `LocalBackend` ↔ `SpiceDBBackend` is a
configuration swap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """Typed reference to a resource. `<resource_type>:<resource_id>`."""

    resource_type: str
    resource_id: str

    def __str__(self) -> str:
        return f"{self.resource_type}:{self.resource_id}"

    @classmethod
    def parse(cls, s: str) -> "ObjectRef":
        if ":" not in s:
            raise ValueError(f"Invalid ObjectRef: {s!r} (expected '<type>:<id>')")
        rt, rid = s.split(":", 1)
        return cls(rt, rid)


@dataclass(frozen=True, slots=True)
class SubjectRef:
    """Typed reference to a subject.

    `subject_type:subject_id[#optional_relation]` — `optional_relation` is the
    SpiceDB subject-set notation (e.g. `auth/group:eng#member`).
    """

    object: ObjectRef
    optional_relation: str = ""

    def __str__(self) -> str:
        if self.optional_relation:
            return f"{self.object}#{self.optional_relation}"
        return str(self.object)

    @property
    def subject_type(self) -> str:
        return self.object.resource_type

    @property
    def subject_id(self) -> str:
        return self.object.resource_id

    @classmethod
    def parse(cls, s: str) -> "SubjectRef":
        rel = ""
        if "#" in s:
            s, rel = s.split("#", 1)
        return cls(object=ObjectRef.parse(s), optional_relation=rel)

    @classmethod
    def of(cls, type_: str, id_: str, relation: str = "") -> "SubjectRef":
        return cls(ObjectRef(type_, str(id_)), relation)


class PermissionResult(str, Enum):
    """Three-state check result."""

    HAS_PERMISSION = "HAS_PERMISSION"
    NO_PERMISSION = "NO_PERMISSION"
    CONDITIONAL_PERMISSION = "CONDITIONAL_PERMISSION"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of a `check_access` call.

    `allowed` is the boolean resolution; `result` carries the three-state value;
    `conditional_on` lists caveat parameter names the caller did not supply (only
    populated when `result == CONDITIONAL_PERMISSION`).
    """

    allowed: bool
    result: PermissionResult = PermissionResult.NO_PERMISSION
    conditional_on: tuple[str, ...] = ()
    reason: Optional[str] = None

    def __bool__(self) -> bool:  # noqa: D401
        return self.allowed

    @classmethod
    def has(cls, reason: str | None = None) -> "CheckResult":
        return cls(True, PermissionResult.HAS_PERMISSION, (), reason)

    @classmethod
    def no(cls, reason: str | None = None) -> "CheckResult":
        return cls(False, PermissionResult.NO_PERMISSION, (), reason)

    @classmethod
    def conditional(cls, missing: tuple[str, ...], reason: str | None = None) -> "CheckResult":
        return cls(False, PermissionResult.CONDITIONAL_PERMISSION, tuple(missing), reason)


class Consistency(str, Enum):
    """Mirrors SpiceDB's `Consistency` requirement values."""

    MINIMIZE_LATENCY = "minimize_latency"
    AT_LEAST_AS_FRESH = "at_least_as_fresh"
    AT_EXACT_SNAPSHOT = "at_exact_snapshot"
    FULLY_CONSISTENT = "fully_consistent"


@dataclass(frozen=True, slots=True)
class Zookie:
    """Consistency token. Encodes `f"{backend_kind}.{xid}"`.

    Tokens are NOT portable across backends — see ARCHITECTURE.md § Migration safety.
    """

    backend: str
    token: str

    def __str__(self) -> str:
        return f"{self.backend}.{self.token}"

    @classmethod
    def parse(cls, s: str) -> "Zookie":
        if "." not in s:
            raise ValueError(f"Invalid Zookie: {s!r}")
        backend, token = s.split(".", 1)
        return cls(backend, token)


@dataclass(frozen=True, slots=True)
class RelationshipTuple:
    """Wire-shape relationship row, matching `authzed.api.v1.Relationship`."""

    resource: ObjectRef
    relation: str
    subject: SubjectRef
    caveat_name: str = ""
    caveat_context: dict[str, Any] = field(default_factory=dict)
    expires_at: Optional[Any] = None  # datetime — typed loosely to keep types.py import-light

    def canonical_key(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            self.resource.resource_type,
            self.resource.resource_id,
            self.relation,
            self.subject.subject_type,
            self.subject.subject_id,
            self.subject.optional_relation,
            self.caveat_name,
        )


@dataclass(frozen=True, slots=True)
class RelationshipFilter:
    """Filter for `delete_relationships` and similar lookups."""

    resource_type: str = ""
    resource_id: str = ""
    relation: str = ""
    subject_type: str = ""
    subject_id: str = ""
    optional_subject_relation: str = ""
