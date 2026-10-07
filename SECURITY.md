# Security policy

Wapsi is decision support for a bank's cyber-fraud nodal officer. It drafts hold instructions, STRs and
restoration packs, and a verifier recomputes the amounts, identifiers, dates and citations in each draft before a
supervisor approves. A flaw in these checks could let a wrong amount, an invented clause or an injected
instruction reach an approved document. We treat reports about them seriously.

This is an MIT-licensed hackathon project maintained on a best-effort basis. There is no bug bounty.

## Supported versions

Only the latest commit on the default branch is supported. There are no maintained release branches.

## How to report a problem

**Please do not open a public issue for a security problem.**

1. Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose
   **Report a vulnerability**.
2. If that option is not available, open a public issue titled "Security contact request" that contains **no
   details**, and a maintainer will arrange a private channel.

Please include:

- what you found and why it matters. For example: "this draft passes `verify_draft` although its hold amount does
  not match the plan";
- the smallest reproduction you can give, built on **synthetic data only**: the showcase fixture
  (`src/wapsi/fixtures/showcase.py`) or a world from `uv run python -m wapsi.synth`;
- the commit you tested and, if relevant, the edition (Snowflake, Google Cloud or AMD).

We will confirm receipt, work out a fix and a disclosure date with you, and credit you if you wish.

## Scope

In scope: anything that defeats a guarantee described in [docs/trust-and-safety.md](docs/trust-and-safety.md).
In particular:

| Area | Examples of an in-scope report |
|---|---|
| Verifier (`src/wapsi/verify/`) | a draft carries an amount, account number, UTR or date the case does not support, and still passes `verify_draft`; a value in the wrong place passes the skeleton check; an unregistered citation passes |
| Policy rules | a hold plan or draft that breaks a hard rule in `AGENTS.md` (over-hold, a whole-account or debit freeze outside an approved exception, forward hold on a pool account, s.106 flag on an innocent holder, an innocent holder named in the automatic STR) passes `verify_plan` or `check_draft_policy` |
| Account-level exception | an `EXCEPTIONAL_ACCOUNT_LEVEL_HOLD` without a recorded reason from the closed `AccountLevelReason` list, without a valid supervisor approval, approved by the officer who requested it, or on a nodal / pool / escrow, merchant, other-bank or victim account, passes the verifier |
| Police / LEA requests (`src/wapsi/verify/lea_request.py`) | the identifiable amount exceeds `min(plan hold, victim money in the cited credits, amount requested)`; text in a request changes the plan or reaches the clarification letter unquoted |
| Audit log (`src/wapsi/audit/`) | an edited, deleted, inserted or reordered event verifies as intact; a truncated or fully re-hashed log verifies against its anchor |
| Prompt injection | complaint text changes a hold plan, reaches an approved draft, or gets past the prose checks in `src/wapsi/draft/prose.py` into a draft |
| Maker-checker (`src/wapsi/pipeline/run.py`) | content is approved that did not pass verification in that exact form; a rejected draft is approved later; one approval creates duplicate actions; a CFCFRMS action is recorded without `payload.interface = "SIMULATED_CFCFRMS"` |
| STR privacy | an innocent account holder is identified in an STR |
| UI (`ui/`) | script injection through complaint or draft text; an auditor can change state or sees unmasked names or mobile numbers |
| Citations and radar | a stored quote in `data/docs/citations.json`, `data/docs/facts.json` or `data/docs/reg_radar.json` does not match its source, yet `uv run python -m wapsi.search.check_citations` passes; the radar changes a plan, clock, draft or citation |
| Editions (`editions/`) | issues in edition-specific code; see each edition's README |

Out of scope (known and documented; ordinary issues are welcome):

- **A `RulesGuard` miss by itself.** The guard is a visibility layer with a measured blind-holdout recall of 0.75
  (`evals/reports/guard-latest.json`). New bypass styles are useful as normal issues with a synthetic example. A
  miss that *also* changes a plan or reaches an approved draft is in scope.
- **The money-grammar gaps already listed** in [docs/trust-and-safety.md](docs/trust-and-safety.md), section 4:
  Hindi number words in Devanagari script, `4.8L`, and "four point eight lakh". A new spelling that gets an
  unsupported amount past `verify_draft` is in scope.
- **The local UI's demo role switcher**, which has no sign-in by design. Identity and access control belong to
  each deployed edition (see [editions/gcp/README.md](editions/gcp/README.md) for signed role grants and IAP).
- Vulnerabilities in third-party services or libraries (Snowflake, Google Cloud, Model Armor, AMD ROCm, vLLM,
  Streamlit, model providers). Please report those to the vendor.
- Denial of service against a demo deployment, social engineering, and physical attacks.

Test only against your own local copy or your own deployment. Do not test against a deployment you do not own.

## No real data

Wapsi uses **synthetic data only**. All banking data comes from `wapsi.synth` or the hand-built showcase fixture.
Synthetic mobile numbers and PANs are generated already masked (`98XXXXXX21`, `XXXXX1234A`), and synthetic bank
names carry "(synthetic)".

- Never put real customer, victim or employer data in a report, an issue, a pull request, a test or a fixture.
  That includes real names, account numbers, NCRP acknowledgement numbers, UTRs, mobile numbers and PANs.
- If you find what looks like real personal data in this repository, report it privately as above. We will
  remove it, including from history where possible.
- Public legal texts are downloaded on demand (`uv run python -m wapsi.search.fetch`) into `data/docs/raw/`,
  which is git-ignored. They are not redistributed beyond the short attributed quotes in
  `data/docs/citations.json` and the short snippets that back the figures in `data/docs/facts.json`.

## Secrets policy

**Never commit a key, token, password or connection file.** Configuration that is secret comes from environment
variables or from the platform's secret store.

| Secret | Read from | Used by |
|---|---|---|
| API key for any OpenAI-compatible endpoint (vLLM, Gemini's OpenAI-compatible API, others) | `WAPSI_LLM_API_KEY` (optional for local servers) | `src/wapsi/adapters/llm_openai_compat.py` |
| Gemini API key (AI Studio backend) | `GEMINI_API_KEY`, or `GOOGLE_API_KEY` | `src/wapsi/adapters/gemini_llm.py` |
| Role-grant signing key (Google Cloud edition) | `WAPSI_ROLE_SIGNING_KEY`, mounted from Secret Manager | [editions/gcp/README.md](editions/gcp/README.md) |
| Snowflake connection | `%USERPROFILE%\.snowflake\connections.toml`, outside the repository | [editions/snowflake/COCO_PLAYBOOK.md](editions/snowflake/COCO_PLAYBOOK.md) |
| AMD edition tokens | the shell environment read by `editions/amd/docker-compose.yml` | [editions/amd/README.md](editions/amd/README.md) |

Rules:

- `.gitignore` excludes `.env`, `.env.*` (except `.env.example`) and `.streamlit/secrets.toml`. Keep local
  secrets there or in your shell profile.
- Tests make no network calls and need no secrets (`AGENTS.md`, convention 7). The CI workflow
  (`.github/workflows/ci.yml`) uses no repository secrets.
- Do not log secrets. The core LLM adapters read their key from the environment and do not log it; error
  messages name the environment variable, not the key. Keep it that way in new adapters.
- Do not paste keys into prompts, notebooks, screenshots or CoCo / Claude Code / Gemini CLI session transcripts
  (`editions/snowflake/coco-sessions/` is committed evidence).

**If a secret is committed:** revoke or rotate it at the provider first. Removing it from git history is not
enough, because clones and forks may already hold it. Then remove it from history and tell the maintainers
privately.

CI has no automated secret scanning. Please review your diff before pushing.

## Related documents

- [docs/trust-and-safety.md](docs/trust-and-safety.md): design principles, verifier, prompt-injection results,
  threat model and known limitations.
- [docs/draft-contract.md](docs/draft-contract.md): the slot, skeleton and body rules the verifier enforces.
- [AGENTS.md](AGENTS.md): the non-negotiable conventions, including the hard policy rules.
