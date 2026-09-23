"""Who is calling, and with which role.

The approval tool reads the caller's role from ADK **session state**, never from model-provided arguments. The
ADK API lets clients write session state (create-session ``state`` and ``events``, PATCH ``state_delta``,
``/run`` ``state_delta``), so a role in state is only trusted when it carries an HMAC grant that binds it to the
session's ``user_id``. Only the server can mint grants (``WAPSI_ROLE_SIGNING_KEY``, from Secret Manager; a random
per-process key otherwise), and ``server.RoleStateMiddleware`` strips any client-supplied ``user:wapsi_*`` keys
before minting a grant from the authenticated identity (IAP header, or a demo header in demo mode).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping, MutableMapping
from typing import Any

ROLE_KEY = "user:wapsi_role"
ROLE_SIG_KEY = "user:wapsi_role_sig"
NAME_KEY = "user:wapsi_name"
PROTECTED_PREFIX = "user:wapsi_"
ROLES = ("analyst", "supervisor", "auditor")
DEFAULT_ROLE = "analyst"

_process_key = secrets.token_bytes(32)
_signing_key: bytes | None = None


def configure_signing_key(key: str | bytes | None) -> None:
    """Set the HMAC key (``None`` = the random per-process key)."""
    global _signing_key
    _signing_key = key.encode("utf-8") if isinstance(key, str) else key


def _key() -> bytes:
    return _signing_key or _process_key


def sign_role(user_id: str, role: str) -> str:
    msg = f"wapsi-role/v1|{user_id}|{role}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()


def grant_role(state: MutableMapping[str, Any], user_id: str, role: str, name: str | None = None) -> None:
    """Write a signed role grant into a session-state mapping (server side only)."""
    role = role.lower()
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    state[ROLE_KEY] = role
    state[ROLE_SIG_KEY] = sign_role(user_id, role)
    state[NAME_KEY] = name or user_id


def granted_state(user_id: str, role: str, name: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    grant_role(out, user_id, role, name)
    return out


def verified_role(state: Mapping[str, Any] | Any, user_id: str) -> str:
    """The caller's role if its grant verifies for ``user_id``; ``analyst`` otherwise."""
    get = state.get
    role = str(get(ROLE_KEY) or "").lower()
    sig = str(get(ROLE_SIG_KEY) or "")
    if role in ROLES and sig and hmac.compare_digest(sig, sign_role(user_id, role)):
        return role
    return DEFAULT_ROLE


def display_name(state: Mapping[str, Any] | Any, user_id: str) -> str:
    return str(state.get(NAME_KEY) or user_id)


def strip_protected(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop client-supplied identity keys from a state dict."""
    return {k: v for k, v in (state or {}).items() if not str(k).startswith(PROTECTED_PREFIX)}


def role_for_email(email: str, supervisors: tuple[str, ...], auditors: tuple[str, ...]) -> str:
    e = email.strip().lower()
    if e in supervisors:
        return "supervisor"
    if e in auditors:
        return "auditor"
    return DEFAULT_ROLE
