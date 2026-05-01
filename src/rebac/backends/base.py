"""Backend ABC. All backends present this surface — see ARCHITECTURE.md § Unified check API."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from ..types import (
    CheckResult,
    Consistency,
    ObjectRef,
    RelationshipFilter,
    RelationshipTuple,
    SubjectRef,
    Zookie,
)


class Backend(ABC):
    """Mirror of `authzed.api.v1` surface, snake_cased."""

    kind: str = ""

    @abstractmethod
    def check_access(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict | None = None,
        consistency: Consistency | None = None,
    ) -> CheckResult:
        """Three-state. Combines model-level and record-level checks."""

    def has_access(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict | None = None,
        consistency: Consistency | None = None,
    ) -> bool:
        """Boolean shorthand. CONDITIONAL collapses to False."""
        return self.check_access(
            subject=subject,
            action=action,
            resource=resource,
            context=context,
            consistency=consistency,
        ).allowed

    @abstractmethod
    def accessible(
        self,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        context: dict | None = None,
        consistency: Consistency | None = None,
    ) -> Iterable[str]:
        """Set of resource_ids the subject has `action` on."""

    @abstractmethod
    def lookup_subjects(
        self,
        *,
        resource: ObjectRef,
        action: str,
        subject_type: str,
        context: dict | None = None,
        consistency: Consistency | None = None,
    ) -> Iterable[SubjectRef]:
        """Reverse: who has `action` on this resource?"""

    @abstractmethod
    def write_relationships(self, writes: Iterable[RelationshipTuple]) -> Zookie:
        ...

    @abstractmethod
    def delete_relationships(self, filter_: RelationshipFilter) -> Zookie:
        ...
