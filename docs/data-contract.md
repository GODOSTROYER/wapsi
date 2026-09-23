# Data contract

## Generated world (Parquet, one file per table, `data/generated/<world>/<table>.parquet`)

Columns map 1:1 to the pydantic models in `src/wapsi/models.py`. Timestamps are `timestamp[us, tz=UTC]`;
dates are `date32`; money is `float64` rupees rounded to 2 decimals; enums are strings.

| Table | Columns | Model |
|---|---|---|
| `banks` | bank_id, name, is_home | `Bank` |
| `customers` | customer_id, name, mobile, pan_masked, kyc_tier, dob, city | `Customer` |
| `accounts` | account_id, bank_id, customer_id, account_type, opened_at, gstin, status, opening_balance | `Account` |
| `transactions` | txn_id, ts, from_account, to_account, from_bank_id, to_bank_id, amount, channel, utr, remark, counterparty_label | `Transaction` |
| `invoices` | invoice_id, merchant_account, payer_account, txn_id, amount, ordered_at, delivered_at, order_status | `Invoice` |
| `gst_registry` | gstin, legal_name, status, registered_at | `GstRecord` |
| `external_signals` | account_id, fri_risk, mulehunter_score, ncrp_prior_reports | `ExternalSignal` |
| `device_links` | device_id, account_id, first_seen | `DeviceLink` |
| `complaints` | ncrp_id, reported_at, victim_account, victim_name, victim_mobile, amount_claimed, narrative_text, language, category, fir_number, source | `Complaint` |
| `complaint_txns` | ncrp_id, txn_id | — |
| `meta` | key, value (world name, seed, sizes, generator version, world_start, world_end, home_bank_id) | — |

### Ground truth (never read by the product; only by evals and classifier training)

| Table | Columns |
|---|---|
| `labels_accounts` | account_id, role_truth (Role), ring_id (nullable), typology (nullable) |
| `labels_flows` | txn_id, ncrp_id, tainted_amount_truth |
| `labels_complaints` | ncrp_id, case_id, ring_id, typology, is_adversarial |

A *case* groups complaints that share first-layer accounts (multi-victim rings → Process 2).
Golden cases: `data/golden/<world>/cases.jsonl`, one `GoldenCase` per line (see `models.GoldenCase`).

### ID formats
- account: `<bank_id>-<10 digits>`, e.g. `HB01-1000000001`; home bank id comes from `meta.home_bank_id` (default `HB01`)
- NCRP acknowledgement: 14 digits
- UTR: 12 digits for UPI/IMPS; `ATM` + 9 digits for cash withdrawals; `CSH` + 9 digits for cash deposits
- device: `DEV-<digits>`; GSTIN: 15-character GSTIN-shaped string (synthetic)

## Visibility rules
- The home bank sees transactions where either side is a home-bank account.
- Other banks' internal transactions are only visible through `Ledger.consortium_transactions` (simulated
  CFCFRMS responses).
- `Ledger.balances_at(ids, ts)` = opening_balance + credits − debits strictly before `ts`.
