-- Wapsi on Snowflake · 61 · Cortex Search service over the legal corpus (run as WAPSI_ADMIN after the chunks are
-- loaded; CREATE builds the index from the current rows). TARGET_LAG 1 day: the corpus changes rarely.
-- Used by the agent tool sop_search, the $sop-citer skill and CortexDocSearch.search().

USE ROLE WAPSI_ADMIN;
USE WAREHOUSE WAPSI_WH;

CREATE OR REPLACE CORTEX SEARCH SERVICE WAPSI.DOCS.SOP_SEARCH
  ON chunk_text
  ATTRIBUTES citation_id, doc_id, section
  WAREHOUSE = WAPSI_WH
  TARGET_LAG = '1 day'
  COMMENT = 'MHA/I4C SOP for NCRP-CFCFRMS + BNSS, PML Rules, RBI, FIU-IND passages with citation ids'
AS (
  SELECT chunk_id, chunk_text, citation_id, doc_id, doc_title, section, source_url, page
  FROM WAPSI.DOCS.DOC_CHUNKS
);

SHOW CORTEX SEARCH SERVICES IN SCHEMA WAPSI.DOCS;
