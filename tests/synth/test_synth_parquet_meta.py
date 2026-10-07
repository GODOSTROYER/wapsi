"""``ParquetLedger.meta``: a public property (mapping) that still answers the older ``meta()`` call."""

from __future__ import annotations

from wapsi.adapters.parquet_ledger import LedgerMeta, ParquetLedger


def test_meta_is_a_property_and_still_callable(showcase_dir):
    pl = ParquetLedger(showcase_dir)
    assert isinstance(pl.meta, LedgerMeta) and isinstance(pl.meta, dict)
    assert pl.meta["size"] == "showcase" and pl.meta.get("generator_version")
    called = pl.meta()
    assert type(called) is dict and called == dict(pl.meta)
    # every read is a copy: callers cannot change the ledger's metadata
    pl.meta["size"] = "tampered"
    called["size"] = "tampered"
    assert pl.meta["size"] == "showcase" and pl.meta()["size"] == "showcase"
