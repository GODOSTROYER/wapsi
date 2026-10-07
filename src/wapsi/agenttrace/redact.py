"""Redaction for agent traces: account numbers, phone numbers, e-mail addresses, names and secrets.

Traces are an observability record that officers, supervisors, auditors and judges look at, so they never carry
a full account number, a phone number, a person's name or a credential. Amounts, NCRP acknowledgement numbers,
case ids and citation ids stay: they are what makes a trace useful and are not personal data.

Untrusted complaint text (anything wrapped in ``<<<UNTRUSTED...>>>`` markers) is never copied into a trace; it is
replaced by a short placeholder that says how long it was.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "•"

# HB01-1000000001 / hb01 1000000001 / HB011000000001 -> HB01-••••••0001 (bank prefix and last four digits kept)
_ACCOUNT_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2}\d{2})([- ]?)(\d{4,8})(\d{4})(?![0-9])")
# Indian mobile numbers: 10 digits (or a masked form like 98XXXXXX21) starting 6-9, optionally +91 / 91- / 0
# prefixed, single spaces or hyphens allowed between digits; never inside a longer digit run (NCRP ids have 14
# digits, UTRs 12, so neither is touched). Masked like the UI does: all but the last two characters.
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+91[\s-]?|91[\s-]|0)?[6-9](?:[\s-]?[0-9X]){9}(?![\w])")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+)")
_UNTRUSTED_RE = re.compile(r"<<<UNTRUSTED[A-Z_]*>>>(.*?)(?:<<<END_UNTRUSTED[A-Z_]*>>>|$)", re.DOTALL)

# keys whose values are personal data or secrets, matched on the lower-cased key
_NAME_KEYS = frozenset({"victim_name", "holder_name", "customer_name", "account_holder", "complainant", "name"})
_PHONE_KEYS = ("mobile", "phone")
_SECRET_KEYS = ("api_key", "apikey", "token", "secret", "password", "authorization", "credential", "signing_key")
_DROP_KEYS = frozenset({"address", "pan", "aadhaar", "email", "dob"})


def mask_name(name: str) -> str:
    """'Meera Joshi' -> 'M•••• J••••' (initials and word lengths kept)."""
    return " ".join(w[0] + MASK * (len(w) - 1) if len(w) > 1 else w for w in str(name).split())


def mask_phone(value: str) -> str:
    """'+91 98765 43210' / '98XXXXXX21' -> '••••••••10' (last two characters, as the UI masks mobiles)."""
    chars = re.sub(r"[^0-9X]", "", str(value))[-10:]
    return MASK * 8 + chars[-2:] if len(chars) > 2 else MASK * len(chars)


def mask_account(text: str) -> str:
    return _ACCOUNT_RE.sub(lambda m: f"{m.group(1).upper()}-{MASK * len(m.group(3))}{m.group(4)}", text)


def redact_text(text: str, *, max_len: int | None = None) -> str:
    """Mask account numbers, phone numbers and e-mails; replace untrusted complaint text by a placeholder."""
    if not text:
        return text
    out = _UNTRUSTED_RE.sub(lambda m: f"[untrusted complaint text, {len(m.group(1))} chars, not copied]", text)
    out = _EMAIL_RE.sub(lambda m: f"{MASK * 3}@{m.group(1)}", out)
    out = mask_account(out)
    out = _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), out)
    if max_len is not None and len(out) > max_len:
        out = out[: max_len - 1] + "…"
    return out


def _key_kind(key: str | None) -> str:
    if not key:
        return ""
    k = key.lower()
    if any(s in k for s in _SECRET_KEYS):
        return "secret"
    if k in _NAME_KEYS or k.endswith("_name") and k not in {"agent_name", "file_name", "tool_name", "app_name"}:
        return "name"
    if any(s in k for s in _PHONE_KEYS):
        return "phone"
    if k in _DROP_KEYS:
        return "drop"
    return ""


def redact_value(value: Any, *, key: str | None = None, max_str: int = 200, max_items: int = 8,
                 max_depth: int = 5, _depth: int = 0) -> Any:
    """JSON-safe, redacted, size-bounded copy of ``value`` (tool arguments and results).

    Strings are redacted and cut to ``max_str``; lists keep ``max_items`` items plus a ``"… +N more"`` marker;
    nesting below ``max_depth`` collapses to a short description.
    """
    kind = _key_kind(key)
    if kind == "secret":
        return "[secret]"
    if kind == "drop" and value not in (None, ""):
        return "[redacted]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return mask_phone(str(value)) if kind == "phone" else value
    if isinstance(value, str):
        if kind == "name":
            return mask_name(value)
        if kind == "phone":
            return mask_phone(value)
        return redact_text(value, max_len=max_str)
    if _depth >= max_depth:
        if isinstance(value, dict):
            return f"{{{len(value)} fields}}"
        if isinstance(value, (list, tuple, set)):
            return f"[{len(value)} items]"
        return redact_text(str(value), max_len=max_str)
    kw = {"max_str": max_str, "max_items": max_items, "max_depth": max_depth, "_depth": _depth + 1}
    if isinstance(value, dict):
        return {str(k): redact_value(v, key=str(k), **kw) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [redact_value(v, key=key if kind else None, **kw) for v in items[:max_items]]
        # no list directly inside a list (Firestore rejects nested arrays): inner lists become one string
        out = [", ".join(map(str, v)) if isinstance(v, list) else v for v in out]
        if len(items) > max_items:
            out.append(f"… +{len(items) - max_items} more")
        return out
    if hasattr(value, "model_dump"):
        try:
            return redact_value(value.model_dump(mode="json"), key=key, **{**kw, "_depth": _depth})
        except Exception:  # noqa: BLE001 - fall through to str()
            pass
    return redact_text(str(value), max_len=max_str)
