"""Snowflake-side layer of Wapsi: case store, Cortex Search DocSearch and the stored-procedure handlers.

Shipped next to the portable ``wapsi`` core in ``@WAPSI.APP.CODE/wapsi.zip`` (see ``../build_zip.py``). Every
handler is a thin wrapper: Snowflake I/O here, all fraud logic in ``wapsi.graph`` / ``wapsi.classify`` /
``wapsi.draft`` / ``wapsi.verify`` / ``wapsi.pipeline``.
"""

__all__ = ["casestore", "docsearch", "procs"]
