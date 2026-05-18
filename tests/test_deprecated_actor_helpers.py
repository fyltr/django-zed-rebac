"""Coverage for the deprecation path in ``rebac.actors`` (proposal 0002).

``accessible_cached`` / ``enable_accessible_cache`` / ``disable_accessible_cache``
are kept as DeprecationWarning-emitting aliases that delegate to the new
evaluator. Removed in 0.6.

The module's `_LEGACY_WARNED` set is process-global "warn once" memo;
each test discards its own entry before exercising the helper so the
warning actually fires under pytest's `filterwarnings = ["error", ...]`
config (which turns the warning into an exception — exactly what we want
to catch).
"""

from __future__ import annotations

import pytest

from rebac import CheckResult, SubjectRef
from rebac.actors import (
    _LEGACY_WARNED,
    accessible_cached,
    disable_accessible_cache,
    enable_accessible_cache,
)
from rebac.evaluator import current_evaluator


class _StubBackend:
    kind = "local"

    def __init__(self) -> None:
        self.calls = 0

    def accessible(self, **_kw: object) -> tuple[str, ...]:
        self.calls += 1
        return ("p1", "p2")

    def check_access(self, **_kw: object) -> CheckResult:
        return CheckResult.has()


def test_accessible_cached_emits_deprecation_warning():
    _LEGACY_WARNED.discard("accessible_cached")
    backend = _StubBackend()
    with pytest.warns(DeprecationWarning, match="evaluator"):
        result = accessible_cached(
            backend,
            subject=SubjectRef.of("auth/user", "1"),
            action="read",
            resource_type="blog/post",
        )
    assert result == ("p1", "p2")


def test_accessible_cached_is_warn_once_per_process():
    """Second call doesn't re-warn — pytest's filterwarnings=error config
    would crash the test if it did. Just calling it twice without
    pytest.warns is the test."""
    _LEGACY_WARNED.discard("accessible_cached")
    backend = _StubBackend()
    # Prime the warning (and consume it).
    with pytest.warns(DeprecationWarning):
        accessible_cached(
            backend,
            subject=SubjectRef.of("auth/user", "1"),
            action="read",
            resource_type="blog/post",
        )
    # Second call: must NOT re-warn (filterwarnings=error would raise).
    accessible_cached(
        backend,
        subject=SubjectRef.of("auth/user", "2"),
        action="read",
        resource_type="blog/post",
    )


def test_enable_disable_accessible_cache_emit_warnings_and_open_scope():
    """The legacy bracket installs an evaluator that ``current_evaluator``
    can see — preserves existing middleware that wraps every request with
    the old enable/disable pair."""
    _LEGACY_WARNED.discard("enable_accessible_cache")
    _LEGACY_WARNED.discard("disable_accessible_cache")
    assert current_evaluator() is None
    with pytest.warns(DeprecationWarning, match="evaluator_scope"):
        token = enable_accessible_cache()
    try:
        # Bracket is open — evaluator exists.
        assert current_evaluator() is not None
    finally:
        with pytest.warns(DeprecationWarning, match="evaluator_scope"):
            disable_accessible_cache(token)
    assert current_evaluator() is None
