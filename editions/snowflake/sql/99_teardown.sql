-- Wapsi on Snowflake · 99 · TEARDOWN after the event. Never run by deploy.py and blocked by the CoCo SQL guard
-- hook ([drop-db-schema]) on purpose: a human runs it in a Snowsight worksheet as ACCOUNTADMIN.
-- Cheaper alternative that keeps the evidence: ALTER WAREHOUSE WAPSI_WH SUSPEND; (and stop here).

USE ROLE ACCOUNTADMIN;
ALTER WAREHOUSE IF EXISTS WAPSI_WH SUSPEND;
DROP DATABASE IF EXISTS WAPSI;
DROP WAREHOUSE IF EXISTS WAPSI_WH;
DROP RESOURCE MONITOR IF EXISTS WAPSI_RM;
