"""Tamper-evident audit log: hash-chained, append-only ``AuditEvent`` logs per case.

    uv run python -m wapsi.audit drill            # tamper drill on the synthetic showcase case
    uv run python -m wapsi.audit verify FILE.json # verify a stored CaseBundle's audit chain

See ``wapsi.audit.chain`` for the hash definition and what is (and is not) detectable without an anchor.
"""

from wapsi.audit.chain import (
    GENESIS_HASH,
    ChainAnchor,
    ChainReport,
    anchor_of,
    append_event,
    chain_extend,
    extend_stored,
    verify_chain,
)

__all__ = [
    "GENESIS_HASH",
    "ChainAnchor",
    "ChainReport",
    "anchor_of",
    "append_event",
    "chain_extend",
    "extend_stored",
    "verify_chain",
]
