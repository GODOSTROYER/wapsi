# AI Builder Cup video: 2:45 (hard limit under 3:00)

**Rule for this cut:** Google Cloud only. No Snowflake, no AMD, no other clouds on screen or in the voice-over.
**Screen:** the live Cloud Run URL (the judged deployment), the ADK tool-call trace, one architecture slide.
**Voice:** one narrator, about 330 words. Every number is a row in [`../NUMBERS.md`](../NUMBERS.md).

**Set-up before recording**
- Judged URL warm (`bash editions/gcp/deploy/cloudrun.sh judging-on`), demo world loaded, signed in as officer in one
  tab and supervisor in another.
- Cases ready: showcase case `31009260000101` (the hold-plan story) and demo case `11208260078296` (a Hinglish
  complaint with an embedded instruction to release holds, from the adversarial test set).
- Crop project ids, URLs with tokens and e-mail addresses in the edit.

---

## 0:00 to 0:20. The number (the whole pitch in 20 seconds)

**Screen:** the KPIs page baseline strip, full width. Two numbers animate side by side:
`Whole-account freeze: ₹35.26 Cr · 101 accounts` and `Wapsi: ₹29.19 L · 84 accounts`. Under them: `0 false freezes`.

**VO:** "When a cyber-fraud victim calls 1930, banks often freeze every account the money touched. On our 39-case
test world, that locks ₹35.26 crore in 101 accounts. Wapsi holds ₹29.19 lakh, only the victim's money, with zero
innocent accounts frozen."

## 0:20 to 0:40. Why it matters (BFSI)

**Screen:** one slide: `₹7,647 Cr held · ₹167 Cr returned · 2.18%` with the source line "MHA/I4C SOP, 02.01.2026".

**VO:** "India held ₹7,647 crore for fraud victims in under five years and returned ₹167 crore. 2.18%. Frozen
accounts mean grievances, delays and innocent customers locked out. Wapsi is the copilot for the bank's nodal officer
who has to fix that, one complaint at a time."

## 0:40 to 1:05. Agent opens the case (Gemini + ADK on Cloud Run)

**Screen:** ADK chat panel. The officer types: `Open the case for NCRP 31009260000101 and show me where the money
went.` The tool-call trace lists `open_case`, `trace_money`, `hold_plan`. The trail graph renders.

**VO:** "The officer just asks. A Gemini agent built with ADK calls Wapsi's tools on Cloud Run. It traces
₹4,80,000 through four layers of accounts, reading the ledger from BigQuery, and every figure it says comes from a
tool, never from the model."

## 1:05 to 1:30. The hold plan

**Screen:** Hold Plan page. Highlight the rows: mules A ₹43,200, B ₹27,648, D ₹48,000; landlord ₹4,800 "no
escalation"; kirana "no hold, goods delivered"; payment-aggregator pool ₹19,200 with its ₹5,00,20,000 balance
untouched. The strip: `₹1,42,848 held · whole-account freeze ₹5,03,50,000`.

**VO:** "Hold only what is left of her money: ₹1,42,848 in five accounts, and ₹3,34,848 recoverable in total. The
landlord keeps his account. The kirana shop delivered its groceries, so no hold. The payment aggregator's pool keeps
running. A whole-account freeze would have locked ₹5,03,50,000."

## 1:30 to 1:55. GenAI with guardrails

**Screen:** Case `11208260078296`: the Hinglish complaint, with the embedded instruction highlighted in red and quoted
back to the officer as untrusted text; the hold plan unchanged. Then Drafts & Verification: STR with the check table
all green, prose from Gemini, numbers from slots.

**VO:** "Complaints arrive in Hinglish, and some try to talk to the machine. This one hides an order to release all
holds. Wapsi quotes it back to the officer and ignores it; the hold stays. Gemini writes the STR's sentences, but every
amount, account and date is filled from data, and a verifier recomputes each one before anyone can approve."

## 1:55 to 2:15. Architecture: all Google Cloud

**Screen:** one diagram: Cloud Run (ADK agent + Streamlit case room) · Gemini · BigQuery ledger · Firestore cases,
approvals and audit · Model Armor and a rules guard · Secret Manager · Cloud Logging.

**VO:** "Two Cloud Run services from one image. Gemini for reasoning, multimodal intake and draft prose. BigQuery
for the ledger, Firestore for cases, approvals and the audit trail. Model Armor and a rules-based guard screen every
complaint. Only a supervisor can approve, and the approval is bound to the exact draft."

## 2:15 to 2:45. Proof and close

**Screen:** evaluation card: `holds exact 1.0 (100%) · over-hold ₹0 · 0 false freezes` / `hard world: 91.98% of
dispositions, holds still exact` / `wrong amount injected into every prose field: 27.57% of drafts replaced, 100%
still verified`. End card: product name, live URL, repository.

**VO:** "We test against exact ground truth. Every hold matches to the paisa. On a harder world built to fool the
classifier, 92% of labels are right, and the misses never touch money. Feed it a model that invents numbers, and not
one wrong amount reaches a draft. Wapsi: give the money back."

---

## Timing check

| Segment | Start | Seconds |
|---|---|---:|
| The number | 0:00 | 20 |
| Why it matters | 0:20 | 20 |
| Agent opens the case | 0:40 | 25 |
| Hold plan | 1:05 | 25 |
| GenAI with guardrails | 1:30 | 25 |
| Architecture | 1:55 | 20 |
| Proof and close | 2:15 | 30 |
| **Total** | | **165 s = 2:45** |

- If Model Armor is not switched on in the judged deployment, say "a rules-based guard" only; do not name Model Armor.
- If the voice-note intake is built before recording (WIN_PLAN improvement 8), swap the 1:30 beat's complaint for a
  Hinglish voice note; otherwise keep the text complaint. Never show a feature the live URL does not have.
- If the Streamlit case room has no tool-trace panel yet, record the 0:40 beat in the ADK dev UI
  (`PUBLIC_API=1`), which shows every tool call and its arguments.
- Upload to YouTube as **public**: the submission package calls for a public video.
