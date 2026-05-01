"""Public exception hierarchy."""
from __future__ import annotations

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied


class ZedRebacError(Exception):
    """Base for all zed_rebac errors."""


class PermissionDenied(DjangoPermissionDenied, ZedRebacError):
    """Raised when an actor is denied access to a resource.

    Subclasses Django's `PermissionDenied` so view-layer 403 handlers continue
    to work without zed_rebac-specific wiring.
    """


class MissingActorError(ZedRebacError):
    """Raised when a queryset materialises with no actor and strict mode is on.

    Strict-by-default: ZED_REBAC_STRICT_MODE = True (production default).
    """


class NoActorResolvedError(ZedRebacError):
    """Resolution chain produced no SubjectRef for the request."""


class CaveatUnsupportedError(ZedRebacError):
    """Caveat references a CEL feature the active backend cannot evaluate.

    For example, the `ipaddress` CEL type is not supported by `cel-python`;
    `LocalBackend` raises this when checking such a caveat. `SpiceDBBackend`
    handles it natively.
    """


class PermissionDepthExceeded(ZedRebacError):
    """Recursive permission walk hit `ZED_REBAC_DEPTH_LIMIT`."""


class SchemaError(ZedRebacError):
    """Schema-level error (parse failure, undefined reference, etc.)."""


class SudoNotAllowedError(ZedRebacError):
    """`sudo()` called when ZED_REBAC_ALLOW_SUDO is False."""


class SudoReasonRequiredError(ZedRebacError):
    """`sudo()` called without `reason=` and ZED_REBAC_REQUIRE_SUDO_REASON is True."""
