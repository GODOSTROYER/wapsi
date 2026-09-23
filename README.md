# Wapsi — money-trail copilot for cyber-fraud restoration

*Wapsi* (वापसी, "return") helps a bank's cyber-fraud nodal officer act on NCRP / 1930 complaints the way the
MHA/I4C **SOP for NCRP-CFCFRMS, Custody, Restoration of money and Grievance Redressal (02.01.2026)** asks:
trace the stolen money through mule layers, **hold only the disputed amount** in each account, keep innocent
account holders and merchants out of harm's way, and get the victim's money back.

> From April 2021 to November 2025, CFCFRMS prevented ₹7,647 crore of crime proceeds out of ₹52,969 crore
> reported — but only **₹167 crore, about 2.18%** of the money saved, has been restored to victims.
> — MHA/I4C SOP, 02.01.2026, printed page 9 (`data/docs/facts.json`)

Courts have been clear that the answer is not to freeze whole accounts: a ₹200 credit froze a ₹93.50 crore
balance in *Neelkanth Pharma Logistics v. UOI* (Delhi HC, 2025), and banks "are empowered only to place the
disputed amount under lien" (*Kartik Yogeshwar Chatur v. UOI*, Bombay HC, 2025).

## What Wapsi does

```
NCRP / 1930 complaint ─► screen untrusted text ─► trace the trail (home bank + CFCFRMS responses)
   ─► taint per account per victim (pro-rata, SOP Para 5(xiii)) ─► classify accounts (mule / innocent / merchant / pool)
   ─► amount-specific hold plan (SOP 9.1, 9.1(i), 9.1(iii), 9.2, 5(xii)) ─► drafts (hold instruction, STR,
      CFCFRMS update, restoration pack P1/P2, EDD memo, notices) ─► verifier (numbers, citations, policy,
      injection) ─► supervisor approval ─► actions + SOP clocks (STR 7 working days, 90-day hold lapse…)
```

- **Numbers come from data, words come from the LLM.** Every amount, account number, UTR and date in a draft
  is a slot filled from case tables; the verifier recomputes each one. The LLM only writes prose, through
  placeholders, and falls back to a fixed paragraph if its output fails the checks.
- **Legal spine in code.** Each disposition cites SOP paragraphs by ID; 58 citation and fact checks verify
  every quote word-for-word against the downloaded documents (`uv run python -m wapsi.search.check_citations`).
- **The SOP's own worked example, reproduced.** Annexure-V Illustration 2 (five victims, one partial debit,
  ₹2,06,543 left) — Wapsi's per-victim shares match the SOP to the paisa (`tests/test_sop_annexure_v.py`).

### Showcase case (`src/wapsi/fixtures/showcase.py`)

A victim loses ₹4,80,000; the money moves through 3 mule layers, a landlord, a kirana shop, a payment-aggregator
pool account, two ATMs and another bank.

| | Whole-account freeze | Wapsi |
|---|---|---|
| Money immobilised at the home bank | ₹5,03,50,000 across 6 accounts (the pool account alone holds ₹5 crore) | ₹1,42,848 across 5 accounts — only the victim's money |
| Landlord who received ₹5,000 rent | whole account frozen | ₹4,800 held, no escalation, EDD memo (SOP 5(xii)) |
| Kirana shop, goods delivered | whole account frozen | no hold, CFCFRMS update only (SOP 9.2) |
| Payment-aggregator pool account | ₹5 crore frozen | ₹19,200 held; no suspension or seizure (SOP 9.1(iii)) |
| Recoverable for the victim | — | ₹3,34,848 (home holds + requests to the other bank) |

### Evaluation (synthetic golden set, `uv run python -m evals.harness --world demo`)

See `evals/reports/latest.json` for the current numbers (schema: `docs/eval-report.md`). The demo world has 39
cases across 10 fraud typologies (fan-out cash-out, chain layering, merchant wash, crypto exit, cross-bank hop,
triangulation, multi-victim, dormant burst, device cluster, nodal/pool), including adversarial complaints with
injected instructions.

## Quick start

```bash
uv sync --all-extras
uv run pytest
uv run python -m wapsi.synth --size demo --seed 7 --out data/generated/demo
uv run python -m wapsi.search.fetch && uv run python -m wapsi.search.build   # public legal corpus (not redistributed)
uv run python -m evals.harness --world demo
uv run streamlit run ui/app.py
```

## Repository

| Path | What |
|---|---|
| `src/wapsi/graph` | trail expansion, exact taint simulation (pro-rata / FIFO / LIFO), hold plan |
| `src/wapsi/classify` | account features, transparent heuristic scorer, optional XGBoost, plain-English reasons |
| `src/wapsi/draft` | fact sheets, 7 document templates, LLM prose with placeholders |
| `src/wapsi/verify` | numeric, citation, policy and prompt-injection checks |
| `src/wapsi/domain` | India bank working-day calendar (DoPT 2026/2027), SOP clocks |
| `src/wapsi/search` | legal corpus fetcher, section-aware chunking, pure-Python BM25 |
| `src/wapsi/synth` | synthetic Indian banking world, independent reference implementation, golden sets |
| `src/wapsi/pipeline` | end-to-end case processing and maker-checker approval |
| `ui/` | Streamlit officer app (Inbox, Case Room, Hold Plan, Drafts & Verification, Approvals, Clocks, KPIs, Audit) |
| `evals/` | golden-set harness |
| `editions/snowflake` | Snowflake-native edition, built with Snowflake CoCo (playbook, skills, hooks) |
| `editions/gcp` | Google Cloud edition (ADK agents on Cloud Run, Gemini, Gemma, BigQuery, Firestore, Model Armor) |
| `.claude/skills` | reusable skills shared by CoCo, Claude Code and Gemini CLI |

## Data and licensing

All banking data is **synthetic** and generated by `wapsi.synth`; no real customer, victim or employer data is
used. Public legal texts are downloaded at build time and are not redistributed beyond short attributed quotes
(`data/docs/MANIFEST.md`). Wapsi is decision support for trained officers, not legal advice. Code is MIT-licensed.
