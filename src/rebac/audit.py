"""Single emission point for `PermissionAuditEvent` rows.

`emit()` is the public entry. Hooks in `actors.py`, `relationships.py`, and
`signals.py` call it; downstream consumers can call it too — re-exported as
`rebac.emit_audit_event`.

`defer_to_commit=True` (default) routes the write through
`transaction.on_commit`, so audit rows for grant / revoke flows persist
exactly when the surrounding business transaction does. `defer_to_commit=False`
writes immediately — correct for events that must persist regardless of
outer transaction state (sudo enter, permission denial about to roll back).
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from .types import SubjectRef


def emit(
    kind: str,
    *,
    actor: SubjectRef | None = None,
    origin: SubjectRef | None = None,
    target_repr: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str = "",
    defer_to_commit: bool = True,
) -> None:
    """Emit a `PermissionAuditEvent`.

    Args:
        kind: One of `PermissionAuditEvent.KIND_*` constants.
        actor: The subject the action ran *as*. May be ``None`` for system
            events (e.g. sudo with no ambient actor).
        origin: The originating subject when actor is reached via
            impersonation / `agents/grant` mediation. The model column is
            not yet defined; the parameter is accepted so callers can wire
            it now and the persistence layer fills in once the schema
            grows the column.
        target_repr: Human-readable target string, e.g.
            ``"drive/file:abc#viewer @ auth/group:eng#member"``.
        before / after: JSON-serialisable diff payloads (optional).
        reason: Free-form reason text (sudo reason, denial reason, ...).
        defer_to_commit: When ``True`` (default), the row is written via
            ``transaction.on_commit`` — i.e. the audit row lands iff the
            surrounding transaction commits (the right semantics for
            grant / revoke audited alongside the relationship rows
            themselves). When ``False``, the row is written immediately —
            the right semantics for ``sudo`` enter (which must persist
            even when not in a transaction) and for permission denials
            (which persist while the would-be save rolls back).
    """
    actor_type = actor.subject_type if actor is not None else ""
    actor_id = actor.subject_id if actor is not None else ""
    # TODO(spec): populate origin from middleware impersonation chain when wired.
    # The PermissionAuditEvent model does not currently carry origin_subject_*
    # columns; the `origin` parameter is accepted for API stability, but until
    # the migration grows the column it's discarded.

    def _write() -> None:
        # Late import — model loading must not block module import time.
        from .models import PermissionAuditEvent

        PermissionAuditEvent.objects.create(
            kind=kind,
            actor_subject_type=actor_type,
            actor_subject_id=actor_id,
            target_repr=target_repr,
            before=before,
            after=after,
            reason=reason,
        )

    if defer_to_commit:
        # `on_commit` runs the callback immediately when called outside an
        # atomic block — that's the correct behaviour for the immediate-write
        # case the caller could have asked for explicitly.
        transaction.on_commit(_write)
    else:
        _write()
