# CoCo GCC deck: 6 slides on the official template

Build it on the 6-slide template from the Hack2Skill dashboard. Keep the template's header (team name, problem
statement 1: Risk, Fraud & Regulatory copilot) and put our content in its body. One idea per slide, numbers in the
first line, a screenshot on every slide from slide 3 on. Every number is a row in [`../NUMBERS.md`](../NUMBERS.md).

Screenshot sources: `editions/snowflake/coco-sessions/screenshots/` (C1 to C4 sessions), the SiS app, and the
local skill output shown in [`VIDEO_SCRIPT.md`](VIDEO_SCRIPT.md).

---

## Slide 1: Problem. 2.18% comes back

**Headline:** ₹7,647 Cr held. ₹167 Cr returned. 2.18%.

**On the slide**
- Big number block: **₹7,647 Cr** held through CFCFRMS, **₹167 Cr** restored, Apr 2021 to Nov 2025 (MHA/I4C SOP,
  02.01.2026, para 2). Latest: more than ₹11,158 Cr saved in 32.80 lakh complaints to 30.06.2026 (MHA reply, PIB).
- Why it is stuck, three icons:
  - **Whole-account freezes** lock innocent landlords, delivered-goods merchants and payment-aggregator pools.
  - **Manual tracing** through layers of mule accounts, per complaint, against SOP deadlines.
  - **Paperwork risk:** an STR within 7 working days, hold notices, restoration packets; one wrong number in a
    regulatory filing is a finding.
- User: the bank's cyber-fraud **nodal officer** handling 1930 / NCRP hold requests.

**Speaker notes (25 s):** "The government's own SOP says it: of ₹7,647 crore held for victims, ₹167 crore went
back. 2.18%. Money is held, but it does not move, and one big reason is that banks freeze whole accounts: every
innocent account frozen becomes a grievance. We built the copilot for the person in the middle: the bank's nodal officer."

---

## Slide 2: Solution. Hold the victim's money, not the account

**Headline:** Trace, hold only the victim's money, draft, verify, approve.

**On the slide**
- Flow strip: `1930 complaint -> money trail -> amount-specific holds -> STR + restoration drafts -> verifier ->
  supervisor approval`.
- Showcase case 1 (hand-computed): ₹4,80,000 stolen. Holds A ₹43,200 · B ₹27,648 · D ₹48,000 · L ₹4,800 ·
  P ₹19,200 = **₹1,42,848** in 5 accounts, ₹96,000 forward hold, ₹1,92,000 requested from the other bank:
  **₹3,34,848 recoverable**. The whole-account freeze baseline locks ₹5,03,50,000 in 6 accounts, 352× more.
- Three rules the officer gets for free:
  - Innocent holder (the landlord, ₹4,800): hold the amount, no escalation, EDD memo; no automatic STR, the officer
    decides.
  - Merchant with delivered goods (kirana, ₹1,152): no hold, update only (SOP 9.2).
  - Payment-aggregator pool: hold the disputed ₹19,200 only; the ₹5,00,20,000 account keeps running (SOP 9.1(iii)).
- Account-level action only as a **recorded, supervisor-approved exception** (4 recorded reasons, maker-checker).
- **Numbers come from data, words come from the LLM.**

**Speaker notes (35 s):** "Wapsi follows the stolen rupees, not the accounts. In our hand-computed case it holds
₹1,42,848 across five accounts and asks the other bank for ₹1,92,000. A whole-account freeze would have locked
more than ₹5 Cr, 352 times more, including a payment aggregator's pool. The LLM writes the sentences. It never writes a
number."

---

## Slide 3: Architecture on Snowflake

**Headline:** Everything runs inside Snowflake; CoCo deploys, tests and guards it.

**Diagram (left to right):**

```
 RAW (synthetic world: accounts, transactions, complaints, invoices, signals)
   -> CASEMGMT Snowpark Python procedures (the portable Wapsi core, packaged as a zip)
        TRACE_CASE -> BUILD_HOLD_PLAN -> DRAFT (Cortex AI_COMPLETE, prose only) -> VERIFY -> APPROVE (supervisor)
   -> DOCS.SOP_SEARCH  Cortex Search over the legal corpus; citation ids as attributes; DOCS.CITATIONS quotes
   -> APP.WAPSI_FRAUD_SV  semantic view (holds, recoverable, clocks, officers)
   -> APP.WAPSI_OFFICER  Cortex Agent: fraud_analyst (Analyst on the semantic view), sop_search, trace_case,
                         build_hold_plan, draft_case. APPROVE is deliberately not a tool.
   -> APP.WAPSI_APP  Streamlit in Snowflake case room (warehouse runtime)
 Governance: roles WAPSI_ANALYST / SUPERVISOR / AUDITOR / LEA_READONLY; secure views (APP.V_*); masking policies
 MASK_PII / MASK_ACCOUNT_ID on Enterprise; ground truth in WAPSI.ML, never granted to app roles.
 CoCo layer: 6 skills ($money-trail-tracer, $lien-calculator, $str-drafter, $sop-citer, $wapsi-eval,
 $synth-fraud-ledger), hooks (SessionStart KPIs, PreToolUse SQL guard, PostToolUse tests), subagents
 (sql-reviewer, sop-auditor, deck-writer).
```

**Callouts**
- **Hallucination control:** AI_COMPLETE fills prose around `⟦slot⟧` placeholders; VERIFY recomputes every amount,
  account, UTR and date (78 numbers in the showcase STR) and checks every citation id against the registry.
- **Guardrails in CoCo:** the PreToolUse hook blocks DROP DATABASE/SCHEMA, any UPDATE or DELETE on AUDIT_LOG /
  APPROVALS, hand-edited hold amounts and grants on ground truth.
- **Cost:** XS warehouse, 60 s auto-suspend, resource monitor, CoCo on Auto Efficient.

**Speaker notes (40 s):** "The core is one Python package. Snowpark runs it unchanged inside Snowflake procedures,
so the numbers in Snowflake equal the numbers in our tests; the C1 parity check shows it. Cortex AI_COMPLETE writes
prose only. Cortex Search grounds every citation. The semantic view and Cortex Agent let the officer ask 'how much is
recoverable across my open cases' in plain English. And CoCo is not a side tool: six skills drive the workflow and a
hook stops CoCo itself from dropping a schema or editing a hold."

**Evidence:** C1 parity table screenshot (holds A ₹43,200 · B ₹27,648 · D ₹48,000 · L ₹4,800 · P ₹19,200 =
₹1,42,848), C2 agent answer, C3 VERIFY table, C4 SiS case room.

---

## Slide 4: Demo flow (the video)

**Headline:** One complaint, four skills, one blocked mistake.

**On the slide:** five screenshots in a row, each with one caption.
1. `$money-trail-tracer`: 11 tainted transfers, ₹4,80,000 reconciled to the rupee.
2. `$lien-calculator`: ₹1,42,848 held in 5 accounts, 48/48 policy checks pass, whole-account freeze baseline
   ₹5,03,50,000 shown for comparison.
3. `$str-drafter`: STR drafted by AI_COMPLETE, 78 numbers recomputed, verification PASS.
4. Verifier: ₹43,200.00 changed to ₹45,000.00 in a hold instruction -> **FAIL, draft withheld**.
5. Guard: CoCo asked to `DROP SCHEMA WAPSI.CASEMGMT` -> **blocked** by the hook.

**Speaker notes (20 s):** "This is the whole case in four skills, run from the CoCo terminal. The last two frames are
the ones we are proudest of: the system catching a wrong number, and stopping a destructive command."

---

## Slide 5: Impact, measured

**Headline:** 0 false freezes. Holds exact to the paisa. Honest where it is hard.

**On the slide**

| Synthetic world | Whole-account freeze baseline | Wapsi holds | Holds exact | False freezes | Dispositions |
|---|---:|---:|---:|---:|---:|
| Demo (39 cases, 10 typologies) | ₹35.26 Cr in 101 accounts | ₹29.19 L in 84 (0.83%) | 1.0 | 0 | 1.0 |
| Hard (64 cases, 15 typologies) | ₹39.55 Cr in 174 | ₹70.73 L in 160 (1.79%) | 1.0 | 0 | 0.9198 |

- Hallucination stress test: a stub LLM writes a wrong rupee amount into every prose field; 27.57% of drafts had
  their prose replaced, drafts passing the verifier still 1.0 (100%).
- Legal spine: 121/121 citation, fact and radar quote checks pass; SOP Annexure-V worked example (₹2,06,543 over five
  victims) reproduced to the paisa.
- Real-LLM row (Cortex AI_COMPLETE on the golden set): **TBD from session C3**. If it is not measured by Oct 3, the
  slide says "stub LLM" and nothing more.

**Speaker notes (30 s):** "Across 39 synthetic cases a whole-account freeze locks ₹35.26 crore; Wapsi holds ₹29.19 lakh,
every rupee of it victim money. On a harder world built to fool the classifier, Wapsi gets 92% of the labels right,
and the misses never touch money: holds stay exact and nothing innocent is frozen."

---

## Slide 6: What's next

**Headline:** From synthetic to a pilot nodal cell.

**On the slide**
- **Regulatory change radar, already built:** 11 rows comparing the RBI DRAFT money-mule SOP (11 Sep 2026; DRAFT,
  comments close 2 Oct 2026, proposed effective 1 Apr 2027) with the MHA SOP, e.g. a 60-day maximum bank-initiated
  hold vs the SOP's 90 days. It flags and suggests; a supervisor approves.
- **Police / LEA requests:** 8-field LEA checklist, hold limited to the identifiable amount, clarification letter
  drafted from a template.
- **Next:** real-LLM A/B on AI_COMPLETE; audit hash chain sealed in Snowflake tables; a pilot with one bank's
  nodal cell on its own Snowflake account; CFCFRMS API integration when access is granted (today a **simulated
  CFCFRMS interface**, labelled on every screen).
- **Datasets and licences:** all case data synthetic (`wapsi.synth`, MIT); legal corpus from public government and
  RBI sources with URLs, hashes and retrieval dates in `data/docs/MANIFEST.md`.

**Speaker notes (20 s):** "Wapsi already reads the next regulation before it lands. Give us one nodal cell and a
Snowflake account, and the next number on this slide is a real one."

---

## Checklist before export

- [ ] `uv run python pitch/check_numbers.py` passes after the last edit.
- [ ] Every screenshot crops account URLs, user names and tokens.
- [ ] No slide says "never freeze"; it says "amount-specific by default; account-level only as a recorded, approved
      exception".
- [ ] The RBI draft is labelled DRAFT wherever it appears.
- [ ] If the template allows an appendix, add a "Datasets & licences" slide with the MANIFEST table.
