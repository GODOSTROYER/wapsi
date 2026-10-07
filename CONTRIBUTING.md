# Contributing to Wapsi

Thank you for helping. Wapsi recommends what a bank does with victims' money, so a contribution is judged
first on whether its numbers and legal statements are right, and only then on anything else.

The [developer guide](docs/developer-guide.md) has the full setup, every command, the architecture and the
testing strategy. This page is the short version.

## Ground rules

1. **Synthetic data only.** Never add real customer, victim, employer or account data, in code, tests,
   fixtures, screenshots or issues. `wapsi.synth` generates everything Wapsi needs.
2. **Never invent a clause.** Every rule cites an ID from `src/wapsi/citations.py`. Each ID is backed by a
   verbatim quote of at most 25 words in `data/docs/citations.json`, except three court judgments known only
   from news reports, which carry a URL and no quote. If the SOP, BNSS, PML Rules or RBI directions do not say
   it, the code does not claim it.
3. **Numbers come from data, words come from the LLM.** Amounts, account numbers, UTRs and dates in drafts are
   template slots; the LLM writes prose through `⟦slot⟧` placeholders only. Do not add a path where model text
   can put a number in a document.
4. **The fixtures are the truth.** `src/wapsi/fixtures/showcase.py` holds hand-computed numbers. If your change
   disagrees with them, your change is wrong, or you must explain in the pull request why the fixture is. Never
   edit a fixture silently.
5. **Hold only the disputed amount.** Changes must keep the hard rules in AGENTS.md convention 6:
   amount-specific holds by default, with account-level action only as a recorded, supervisor-approved exception
   (`EXCEPTIONAL_ACCOUNT_LEVEL_HOLD`, never emitted by the hold plan); forward holds only on first-layer
   accounts; no suspension or seizure of nodal / pool / escrow accounts; no escalation and no automatic STR for
   innocent holders (the officer decides, and an STR is recommended where the account was used as a conduit);
   s.106 escalation only as a flag for a human.
6. **Untrusted text stays untrusted.** Complaint narratives, invoices and pasted text are quoted to the officer
   and never acted on.
7. **Coordinate before changing shared contracts:** `src/wapsi/models.py`, `src/wapsi/ports.py` and
   `pyproject.toml` are used by every module and every edition.

`AGENTS.md` is the authoritative list of conventions. Coding agents (Claude Code, Gemini CLI) read it too.

## Set up

```bash
uv sync --all-extras                   # what CI runs; includes PyTorch and the Google Cloud SDKs
uv run pytest -q
```

For work on the portable core only, the light install is much smaller. `tests/amd` then has to be left out,
because several of its modules import PyTorch directly:

```bash
uv sync --extra ml --extra docs --extra ui
uv run pytest --ignore=tests/gcp --ignore=tests/amd -q
```

On Windows, set `PYTHONUTF8=1` first (see the [developer guide](docs/developer-guide.md#windows-notes)).

## Before you open a pull request

Run the same checks as CI (`.github/workflows/ci.yml`), and format the files you changed. The `--out` option
keeps the committed `evals/reports/latest.json` untouched:

```bash
uv run ruff format <the files you changed>
uv run ruff check src tests evals ui
uv run pytest                          # with the light install: add --ignore=tests/gcp --ignore=tests/amd
uv run python -m wapsi.synth --size showcase --out data/generated/showcase
uv run python -m wapsi.synth --size demo --seed 7 --out data/generated/demo
uv run python -m evals.harness --world showcase --out /tmp/wapsi-reports
uv run python -m evals.harness --world demo --out /tmp/wapsi-reports
```

Do not run `ruff format` on the whole tree: CI checks lint only, and 34 files in `src` and `tests` (among them
`models.py`, `ports.py` and the showcase fixture) are not in `ruff format` style, so a tree-wide format fills
your pull request with unrelated changes to shared contracts.

On Windows, `/tmp/wapsi-reports` works in Git Bash; in PowerShell use any scratch folder outside the
repository.

Then, depending on what you touched:

| You changed | Also do |
|---|---|
| tracing, taint or the hold plan (`src/wapsi/graph/`) | make sure `tests/graph`, `tests/test_sop_annexure_v.py` and the showcase tests pass unchanged; compare the harness report with `evals/reports/latest.json` |
| a disposition or policy rule | follow [Adding a disposition rule safely](docs/developer-guide.md#adding-a-disposition-rule-safely): planner, verifier and reference together, with tests |
| the generator or `wapsi.synth.reference` | regenerate the showcase and demo golden sets and explain every changed line of `data/golden/*/cases.jsonl` |
| `data/docs/citations.json` | `cp data/docs/citations.json src/wapsi/_data/citations.json`, then `uv run python -m wapsi.search.check_citations` (needs the raw documents) |
| `data/docs/reg_radar.json` (regulatory radar) | keep every quote verbatim (≤ 25 words); `uv run python -m wapsi.search.radar verify` (after `radar fetch`), then `uv run python -m wapsi.search.check_citations`; rows stay `review.status = "pending"` until a supervisor reviews them |
| the audit log (`src/wapsi/audit/`) or a case store | append events with `wapsi.audit.append_event`, never `bundle.audit.append`; `uv run pytest tests/audit` and `uv run python -m wapsi.audit drill` (must print PASS) |
| a draft template or the drafter | `tests/draft` and `tests/verify` pass; update [draft-contract.md](docs/draft-contract.md) if a slot or draft kind changed |
| the guard (`wapsi.verify.injection`) | `uv run python -m evals.adversarial.run` (add `--out <scratch file>` while experimenting; the default rewrites `evals/reports/guard-latest.json`); tune on the dev split only and report the test and blind-holdout numbers |
| a skill in `.claude/skills/` | `uv run python scripts/sync_skills.py`, then `--check` |
| an edition (`editions/<name>/`) | keep platform code out of the core; tests go in `tests/<name>/` and skip when the SDK is missing |

Commit `evals/reports/latest.json` only when a change is meant to move the published numbers, and say so.

## Pull request checklist

- [ ] Tests added or updated; the suite passes locally.
- [ ] `ruff check` is clean and the files you changed are formatted.
- [ ] No real personal data anywhere.
- [ ] Every new legal statement has a citation ID and a verbatim quote.
- [ ] Showcase and Annexure-V numbers unchanged, or the change is explained.
- [ ] Harness metrics on the demo world did not get worse: `hold_exact_match_rate`, `disposition_accuracy`,
      `draft_verification_pass_rate`, `over_hold_inr` (must stay ₹0.00), `false_freeze_count` (must stay 0),
      `injection_leaks` (must stay 0).
- [ ] Documentation updated (the relevant page under `docs/`, AGENTS.md for conventions).

Describe what changed and why, and paste the relevant test or harness output. Keep pull requests small: one
behaviour change per pull request is easiest to review against the SOP.

## Style

- Python 3.11+, type hints, `from __future__ import annotations`, a module docstring that states the contract.
- Ruff: line length 110, rules `E`, `F`, `I`, `UP`, `B`.
- Money with `wapsi.money` (`r2`, `eq`, `fmt_inr`); time with `wapsi.timeutil` (store UTC, show IST).
- Every verification check carries a stable machine code in `CheckResult.code` (`over_hold`, `slot_mismatch`,
  `ok`); the message is plain text (`wapsi.verify.report.code_of` also reads older `"code: message"` records).
- Import heavy or optional dependencies inside the function that needs them.
- No network calls in tests: use `httpx.MockTransport` or an offline fake.
- Test files are named `tests/<area>/test_<area>_<topic>.py`, because most test folders have no `__init__.py`
  and pytest needs unique basenames.
- Documentation: plain English, short paragraphs, real numbers from the repository, rupees in Indian format
  (`₹4,80,000.00`).

## Event rules

Wapsi is entered in two hackathons. Follow "Originality and event boundaries" in AGENTS.md and
[docs/originality.md](docs/originality.md):

- AI Builder Cup: only work created on or after 2026-09-01 counts. Work on Wapsi started on 2026-09-16; the
  first commit is dated 2026-09-23.
- AMD ACT III: every run on AMD hardware and every reported GPU number happens during 12–18 Oct 2026. CPU
  development numbers are labelled as such.
- Gemini outputs are not used to train other models.
- Never rewrite commit dates.

## Reporting problems

Open an issue with the command you ran, the full output and, if possible, a small synthetic world that shows
the problem (`tests/graph/graph_worlds.py` makes this easy). If you find a way to make Wapsi hold more than
the victims' money, name an innocent holder in an STR, or act on text injected into a complaint, treat it as a
security problem: report it privately as described in [SECURITY.md](SECURITY.md), not in a public issue.

## Licence

Wapsi is released under the [MIT licence](LICENSE). By contributing you agree that your contribution is
released under the same licence.
