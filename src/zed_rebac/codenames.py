"""Codename → REBAC action mapping.

Used by the Django auth backend to translate `user.has_perm("blog.view_post")`
into a REBAC `read` check.
"""
from __future__ import annotations

DEFAULT_CODENAME_MAP: dict[str, str] = {
    "view": "read",
    "change": "write",
    "delete": "delete",
    "add": "create",
}


def codename_to_action(perm: str, *, overrides: dict[str, str] | None = None) -> str | None:
    """Map a Django permission codename like `blog.view_post` to a REBAC action.

    Returns None when the codename does not match a known prefix; the auth
    backend interprets None as "let the next backend handle this".
    """
    overrides = overrides or {}
    if perm in overrides:
        return overrides[perm]

    if "." in perm:
        _, codename = perm.split(".", 1)
    else:
        codename = perm

    if "_" not in codename:
        return None
    prefix, _ = codename.split("_", 1)
    return DEFAULT_CODENAME_MAP.get(prefix)
