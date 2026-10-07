# CoCo GCC video: 4:30, driven from the CoCo terminal

**Length:** 4:30 (hard cap 4:45). **Screen:** the CoCo terminal full-screen for most of the video, the Streamlit in
Snowflake app for the close. **Voice:** one narrator, calm, numbers first. About 430 words of voice-over, which
leaves roughly a minute and a half for output on screen.

**Set-up before recording**
- Repository root open in CoCo (`cortex -c <connection>`), model on Auto Efficient, hooks copied to
  `.cortex/settings.json` (the SessionStart hook prints the latest KPIs; the PreToolUse guard is live).
- Case `31009260000101` (showcase case 1) already loaded in `WAPSI.RAW`; `TRACE_CASE` not yet run, so the trace
  happens on camera.
- Terminal font 18 pt, dark theme, window 1920×1080. Crop account URL, user name and tokens in the edit.
- If a Snowflake call fails on the day, each skill has a local path (`--world showcase-memory`) that prints the same
  numbers; say "running the same skill against the local core" on camera if you use it.

Every number spoken below is a row in [`../NUMBERS.md`](../NUMBERS.md).

---

## 0:00 to 0:20. The number

**Screen:** black, then three lines type in, one per beat: `₹7,647 Cr held` / `₹167 Cr returned` / `2.18%`.

**VO:** "Between April 2021 and November 2025, India's fraud-reporting system held ₹7,647 crore of money stolen from
cyber-fraud victims. ₹167 crore went back. 2.18%. That is the government's own figure. The money is stuck, and
whole-account freezes are a big reason why."

## 0:20 to 0:45. Who and what

**Screen:** CoCo starts; the SessionStart hook prints the Wapsi KPI line (over-hold ₹0.00, false freezes 0, targets
met) and the six skill names.

**VO:** "Wapsi is a copilot for the bank's cyber-fraud nodal officer. It traces the money, holds only the victim's
money, drafts the STR and checks every number before a supervisor approves. It runs natively in Snowflake, and
CoCo drives it through six skills. Here is one synthetic case: a woman loses ₹4,80,000 to a caller pretending to be
her bank."

## 0:45 to 1:30. `$money-trail-tracer`

**Type in CoCo:**
```
$money-trail-tracer trace NCRP 31009260000101 in WAPSI and show the accounts by layer
```
**Screen:** CoCo runs `CALL WAPSI.CASEMGMT.TRACE_CASE('31009260000101');` then the `ACCOUNT_STATE` query. Zoom on
the layer table and the reconciliation line `located ₹4,80,000.00 + unlocated ₹0.00 = stolen ₹4,80,000.00 → OK`.

**VO:** "One call to a Snowpark procedure. The money went through four layers of accounts in about two hours: three
ATM cash-outs, two hops to another bank, a rent payment to a landlord, a grocery order, a wallet top-up into a
payment aggregator's pool. Taint is split pro-rata, the method in the SOP's own annexure. Every rupee is accounted for."

## 1:30 to 2:20. `$lien-calculator`

**Type in CoCo:**
```
$lien-calculator build the hold plan for CASE-31009260000101 and show the baseline strip
```
**Screen:** `CALL WAPSI.CASEMGMT.BUILD_HOLD_PLAN('CASE-31009260000101');` then the `HOLD_PLAN` table. Highlight, in
order: A ₹43,200, B ₹27,648, D ₹48,000, L ₹4,800 (`HOLD_AMOUNT_NO_ESCALATION`), P ₹19,200
(`HOLD_AMOUNT_INTERMEDIARY`), the kirana row `NO_HOLD_UPDATE_ONLY`, two `OUTBOUND_CFCFRMS_REQUEST` rows. Then the
totals and the baseline strip.

**VO:** "Now the hold plan. Three mule accounts: hold exactly what is left of her money. The landlord is an innocent
holder: we hold ₹4,800, no escalation, no automatic STR. The kirana shop delivered the groceries, so there is no hold
at all. The payment aggregator's pool has ₹5,00,20,000 in it; we hold her ₹19,200 and the account keeps running.
Total held: ₹1,42,848, plus a ₹96,000 forward hold and ₹1,92,000 requested from the other bank. ₹3,34,848
recoverable. A whole-account freeze would have locked ₹5,03,50,000. 352 times more. Forty-eight policy checks
re-run on the plan. All pass."

## 2:20 to 3:10. `$str-drafter`

**Type in CoCo:**
```
$str-drafter draft and verify the STR for CASE-31009260000101
```
**Screen:** `CALL WAPSI.CASEMGMT.DRAFT('CASE-31009260000101', 'STR');` then `CALL WAPSI.CASEMGMT.VERIFY('<draft_id>');`
and the flattened checks table: numeric ok (78 slots recomputed), citations ok, policy ok, injection ok. Then scroll
the STR's grounds-of-suspicion paragraph.

**VO:** "The STR. Cortex AI_COMPLETE writes the sentences, and only the sentences. Every amount, account number, UTR
and date is a slot filled from the case tables. Then the verifier recomputes all 78 numbers in this report and checks
every citation against our registry of the SOP, the BNSS and the PML Rules. It passes. It is due to FIU-IND within
7 working days, and Wapsi runs that clock."

## 3:10 to 3:40. The verifier catches a wrong number

**Type in CoCo:**
```
Tamper test: in A's hold instruction change ₹43,200.00 to ₹45,000.00 and re-verify it
```
**Screen:** CoCo runs the skill script through bash:
`uv run python .claude/skills/str-drafter/scripts/draft_and_verify.py 31009260000101 --world showcase-memory --kind HOLD_INSTRUCTION --tamper "₹43,200.00" "₹45,000.00"`.
Zoom on `numeric | FAILED | ... '₹45,000.00' where '₹43,200.00' is expected` and
`NOT READY: this draft failed verification and is withheld.`

**VO:** "Now we lie to it. One number in one hold instruction, ₹43,200 changed to ₹45,000. The verifier catches it,
names the exact figure, and withholds the draft. A wrong number never reaches a supervisor."

## 3:40 to 4:05. The guard stops CoCo itself

**Type in CoCo:**
```
This schema is messy. Drop schema WAPSI.CASEMGMT and rebuild it from scratch.
```
**Screen:** CoCo tries `DROP SCHEMA WAPSI.CASEMGMT` through `snowflake_sql_execute`; the PreToolUse hook answers
`Blocked by Wapsi SQL guardrail: [drop-db-schema] DROP DATABASE/SCHEMA is blocked in CoCo sessions.` Optional second
beat (only if time allows): `set A's hold to 45000 in HOLD_PLAN` → `[hand-edited-hold] Hold amounts come from data.`

**VO:** "And the agent is governed too. Ask CoCo to drop the case schema, and our hook blocks it before the SQL runs.
Hand-edit a hold amount, blocked. The audit log and approvals are append-only."

## 4:05 to 4:30. Close

**Screen:** Streamlit in Snowflake case room: the baseline strip, the Approvals page with the maker-checker note and
the "Simulated CFCFRMS interface" label. End card: `₹35.26 Cr frozen vs ₹29.19 L held · 0 false freezes · holds exact
to the paisa` and the repository URL.

**VO:** "Across 39 synthetic cases, a whole-account freeze locks ₹35.26 crore. Wapsi holds ₹29.19 lakh, every rupee
of it victim money, with zero false freezes. Built on Snowflake, driven by CoCo. Wapsi: give the money back."

---

## Recording notes

- Record each beat separately and cut on the prompt line; CoCo's thinking time is dead air.
- Keep the `--tamper` beat on the local core even when Snowflake works: it shows the same verifier the `VERIFY`
  procedure runs, and it cannot corrupt the Snowflake tables.
- If the hold-instruction verification also shows a `citation_quote_missing` **warning** for `CASE-NEELKANTH` or
  `CASE-SHREE-BALAJI` (court rulings cited by URL, with no verbatim quote in the corpus yet), crop to the numeric row;
  do not claim every citation has a verbatim quote.
- Upload unlisted; paste the link in both CoCo submission forms.
