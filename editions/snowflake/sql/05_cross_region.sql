-- Wapsi on Snowflake · 05 · OPTIONAL: Cortex cross-region inference (run as ACCOUNTADMIN).
-- Only needed when the account's region does not serve the chosen AI_COMPLETE / agent model natively
-- (e.g. ap-southeast-1). Not part of `--step all`; run it explicitly:
--   uv run python -m editions.snowflake.deploy --step cross-region
-- Inference may then run in another region; say so in the deck. The SQL guard hook allows exactly this
-- ALTER ACCOUNT parameter (CORTEX_ENABLED_CROSS_REGION) and blocks every other one.

USE ROLE ACCOUNTADMIN;
ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'ANY_REGION';
SHOW PARAMETERS LIKE 'CORTEX_ENABLED_CROSS_REGION' IN ACCOUNT;
