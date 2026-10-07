# Wapsi: start here (judges)

**Wapsi** (Hindi: *return*) is a copilot for a bank's cyber-fraud nodal officer. It takes a 1930 / NCRP complaint,
traces the stolen money through mule accounts, **holds only the victim's money in each account**, drafts the STR
and the restoration packet, and **recomputes every number and citation** before a supervisor approves.

## The 60-second hook

> **₹7,647 crore held. ₹167 crore returned. 2.18%.**
> That is what CFCFRMS achieved for cyber-fraud victims from April 2021 to November 2025, in the government's own
> words (MHA/I4C SOP, 02.01.2026, para 2).
>
> One big reason the money is stuck: banks freeze **whole accounts**. The landlord whose rent payment carried ₹4,800
> of victim money, the kirana shop paid ₹1,152 of it for groceries it delivered, the payment-aggregator pool holding
> ₹5,00,20,000 of other people's money: all locked, all grievances in the making, while the victim waits.
>
> On our 39-case synthetic test world a whole-account freeze locks **₹35.26 crore in 101 accounts**. Wapsi holds
> **₹29.19 lakh in 84**, which is 0.83% of it. Every rupee it holds is victim money: **0 false freezes, over-hold ₹0,
> every hold exact to the paisa.** The LLM never types a number; a verifier recomputes all of them.

## Headline numbers

Freeze baseline against Wapsi. The baseline is the balance of every home-bank account that received victim money:
a comparison figure, never a recommendation.

| World | Whole-account freeze baseline | Wapsi holds | Share | Source |
|---|---:|---:|---:|---|
| Showcase case 1 (hand-computed; ₹4,80,000 stolen) | ₹5,03,50,000 in 6 accounts | ₹1,42,848 in 5 accounts | the freeze locks 352× more | [`showcase.py`](../src/wapsi/fixtures/showcase.py) |
| Showcase world (2 cases, 3 victims) | ₹5,04,70,000 (₹5.05 Cr) in 7 | ₹2,62,848 in 6 | 0.52% | [`showcase.json`](../evals/reports/showcase.json) |
| **Demo world (39 cases, 10 typologies)** | **₹35,25,90,069.78 (₹35.26 Cr) in 101** | **₹29,19,245.69 (₹29.19 L) in 84** | **0.83%** | [`demo.json`](../evals/reports/demo.json) |
| Hard world (64 cases, 15 typologies) | ₹39,55,06,565.52 (₹39.55 Cr) in 174 | ₹70,72,636.72 (₹70.73 L) in 160 | 1.79% | [`hard.json`](../evals/reports/hard.json) |

Showcase case 1 in one line: ₹1,42,848 held now, ₹96,000 forward hold on the first mule, ₹1,92,000 requested from
the other bank, **₹3,34,848 recoverable (69.76% of the stolen money)**, restoration Process 1.

| Metric (golden set) | Showcase | Demo | Hard (seed 7) | Hard-101 |
|---|---:|---:|---:|---:|
| Holds exact to the paisa | 1.0 | 1.0 | 1.0 | 1.0 |
| Over-hold / false freezes | ₹0 / 0 | ₹0 / 0 | ₹0 / 0 | ₹0 / 0 |
| Disposition accuracy | 1.0 | 1.0 | **0.9198** | **0.9139** |
| STR-ground accuracy | 1.0 | 1.0 | 0.875 | 0.9032 |
| Innocent-holder recall | 1.0 | 1.0 | 0.6875 | 0.68 |
| Drafts passing the verifier | 1.0 | 1.0 | 1.0 | 1.0 |
| Citation validity / injection leaks | 1.0 / 0 | 1.0 / 0 | 1.0 / 0 | 1.0 / 0 |

**What the hard world shows.** It adds five confounders a nodal officer really struggles with (sleeper mules,
task-scam victim chains, a labour contractor's salary pool, a festival-season home seller, same-second relays).
Wapsi gets 91.98% of dispositions right there, and **every miss is a label, never money**: holds stay exact, over-hold
₹0, false freezes 0. A wrong label changes the paperwork (EDD memo instead of an STR ground, or the reverse), and a
supervisor approves every draft.

**Hallucination stress test.** A stub LLM that writes a wrong rupee amount into every prose field: 27.57% of drafts
had their prose rejected and replaced by fixed text, drafts passing the verifier still 1.0 (100%), holds still exact
([`demo-fake-hallucinate.json`](../evals/reports/baseline/demo-fake-hallucinate.json)).

**Prompt-injection guard, the honest number.** On a blind holdout of 151 texts written by a separate author:
blind-holdout precision 0.9661, blind-holdout recall 0.75 ([`guard-latest.json`](../evals/reports/guard-latest.json)).
The guard is a warning layer. The defence is structural: the hold plan, not the LLM, decides every action.

**Measured so far with the deterministic stub LLM.** Real-LLM A/B runs (Cortex AI_COMPLETE, Gemini, Gemma 4 on
MI300X) fill a slot in each edition's deck; until then they read **TBD**. All case data is synthetic.

## Try it in 2 minutes

From the repository root (Git Bash, macOS or Linux; [uv](https://docs.astral.sh/uv/) installed):

```bash
uv sync --extra ui                                          # 1. install (core + Streamlit UI)

# 2. trace the showcase complaint: 11 tainted transfers, ₹4,80,000 reconciled to the rupee
uv run python .claude/skills/money-trail-tracer/scripts/trace.py 31009260000101 --world showcase-memory

# 3. hold plan: A ₹43,200 · B ₹27,648 · D ₹48,000 · L ₹4,800 · P ₹19,200 = ₹1,42,848; 48/48 policy checks
uv run python .claude/skills/lien-calculator/scripts/hold_plan.py 31009260000101 --world showcase-memory

# 4. the verifier catches a tampered amount (₹43,200.00 -> ₹45,000.00): FAIL, draft withheld, exit 1
uv run python .claude/skills/str-drafter/scripts/draft_and_verify.py 31009260000101 --world showcase-memory \
  --kind HOLD_INSTRUCTION --tamper "₹43,200.00" "₹45,000.00"

# 5. the Snowflake SQL guard blocks a DROP (exit 2): the PreToolUse hook configured for CoCo's SQL and bash tools
echo '{"tool_name":"snowflake_sql_execute","tool_input":{"sql":"DROP SCHEMA WAPSI.CASEMGMT"}}' \
  | uv run python editions/snowflake/hooks/pre_sql_guard.py

# 6. audit log tamper drill: 6/6 tampering attempts detected on a hash-chained case log
uv run python -m wapsi.audit drill

# 7. golden-set evaluation on the showcase world (writes evals/reports/latest.json)
uv run python -m wapsi.synth --size showcase --out data/generated/showcase
uv run python -m evals.harness --world showcase

# 8. the officer UI: Inbox, Case Room, Hold Plan, Drafts & Verification, Approvals, Clocks, Regulatory Radar, KPIs, Audit
uv run streamlit run ui/app.py
```

Then check the pitch itself: `uv run python pitch/check_numbers.py` re-reads every source behind every number in
this folder.

## What to look at, by question

| Question | File |
|---|---|
| What rules does it follow? | [`AGENTS.md`](../AGENTS.md) (convention 6), [`src/wapsi/citations.py`](../src/wapsi/citations.py), [`data/docs/citations.json`](../data/docs/citations.json) |
| Where do the hand-computed numbers come from? | [`src/wapsi/fixtures/showcase.py`](../src/wapsi/fixtures/showcase.py) (worked out in its docstring) |
| How are holds computed? | [`src/wapsi/graph/holdplan.py`](../src/wapsi/graph/holdplan.py), [`src/wapsi/graph/trace.py`](../src/wapsi/graph/trace.py) |
| How is the LLM kept away from numbers? | [`src/wapsi/draft/`](../src/wapsi/draft/) (slot templates), [`src/wapsi/verify/`](../src/wapsi/verify/) (numeric, citation, policy, injection checks) |
| Account-level holds as a supervisor-approved exception | `request_account_level_hold` / `approve` in [`src/wapsi/pipeline/run.py`](../src/wapsi/pipeline/run.py); 4 recorded reasons in [`src/wapsi/models.py`](../src/wapsi/models.py) |
| Bona fide recipient vs unwitting conduit | [`src/wapsi/classify/rules.py`](../src/wapsi/classify/rules.py), [`src/wapsi/verify/policy.py`](../src/wapsi/verify/policy.py) |
| Police / LEA hold requests | [`src/wapsi/verify/lea_request.py`](../src/wapsi/verify/lea_request.py) (8-field LEA checklist, identifiable amount, clarification letter) |
| Tamper-evident audit log | [`src/wapsi/audit/`](../src/wapsi/audit/) (hash chain, `python -m wapsi.audit drill`) |
| Regulatory change radar (RBI DRAFT vs the SOP) | [`data/docs/reg_radar.json`](../data/docs/reg_radar.json), [`src/wapsi/domain/regradar.py`](../src/wapsi/domain/regradar.py) |
| How is it evaluated, independently? | [`evals/harness.py`](../evals/harness.py), [`src/wapsi/synth/reference.py`](../src/wapsi/synth/reference.py) (shares no code with the hold plan), [`tests/test_sop_annexure_v.py`](../tests/test_sop_annexure_v.py) |
| Official statistics | [`data/docs/facts.json`](../data/docs/facts.json) (verbatim quotes, page numbers) |
| Every number in this pitch | [`pitch/NUMBERS.md`](NUMBERS.md) |

## By event

| Event | Edition | Pitch material |
|---|---|---|
| Snowflake CoCo CLI Hackathon (GCC edition), problem statement 1 | [`editions/snowflake/`](../editions/snowflake/README.md) | [`pitch/coco/`](coco/) |
| AI Builder Cup 2026 (Google Cloud), BFSI | [`editions/gcp/`](../editions/gcp/README.md) | [`pitch/aibc/`](aibc/) |
| AMD Developer Hackathon ACT III | [`editions/amd/`](../editions/amd/README.md) | [`pitch/amd/`](amd/) |

## Limits we state up front

- All banking data is synthetic, generated by `wapsi.synth`. Official statistics are real and quoted verbatim.
- Wapsi is not connected to the CFCFRMS portal. Every CFCFRMS action is a **simulated CFCFRMS interface** and says so.
- The RBI draft SOP on money-mule accounts (11 Sep 2026) is a **DRAFT**; Wapsi flags what it would change and never
  applies it.
- Draft metrics above use the deterministic stub LLM; real-LLM numbers are TBD per edition.
