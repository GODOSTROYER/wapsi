-- Wapsi on Snowflake · 60 · legal corpus tables (run as WAPSI_ADMIN). deploy.py --step search loads them from
-- the local docs index (data/docs/index/chunks.jsonl, or the citation-quote corpus when the index is not built)
-- and the citation registry (wapsi.citations), then creates the search service (61_search.sql).
-- Chunks are <= 900 characters (well under Cortex Search's 512-token guidance).

USE ROLE WAPSI_ADMIN;

CREATE TABLE IF NOT EXISTS WAPSI.DOCS.DOC_CHUNKS (
  CHUNK_ID    VARCHAR NOT NULL,
  CHUNK_TEXT  VARCHAR,
  CITATION_ID VARCHAR,
  DOC_ID      VARCHAR,
  DOC_TITLE   VARCHAR,
  SECTION     VARCHAR,
  SOURCE_URL  VARCHAR,
  PAGE        NUMBER
) COMMENT = 'Public legal / regulatory corpus chunks (MHA/I4C SOP, BNSS, PML Rules, RBI, FIU-IND)';

CREATE TABLE IF NOT EXISTS WAPSI.DOCS.CITATIONS (
  CITATION_ID  VARCHAR NOT NULL,
  SOURCE_TITLE VARCHAR,
  SECTION      VARCHAR,
  QUOTE        VARCHAR,
  URL          VARCHAR,
  PAGE         NUMBER
) COMMENT = 'The only citable ids (wapsi.citations registry) with verbatim quotes (<= 25 words)';
