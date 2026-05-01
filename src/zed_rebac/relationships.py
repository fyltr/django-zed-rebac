"""Public helpers `write_relationships` / `delete_relationships`."""
from __future__ import annotations

from typing import Iterable

from .types import RelationshipFilter, RelationshipTuple, Zookie


def write_relationships(writes: Iterable[RelationshipTuple]) -> Zookie:
    """Atomically commit relationship rows. Returns a consistency token."""
    from . import backend
    return backend().write_relationships(writes)


def delete_relationships(filter_: RelationshipFilter) -> Zookie:
    """Atomically delete matching relationship rows."""
    from . import backend
    return backend().delete_relationships(filter_)
