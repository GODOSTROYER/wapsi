# Track fit: Reinvent Commerce (Track 3)

AMD Developer Hackathon ACT III, challenge "Solve a real Business Problem with AMD technology". We enter
**Track 3, Reinvent Commerce: "Help a business understand its customers and take better action."**

This page sets each line of the track brief (as read on the event page on 2026-10-14) against the evidence in this
repository. Every number is a row in [`NUMBERS.md`](NUMBERS.md); `TBD(slot_id)` marks a figure that is not
measured yet and is not claimed. All banking data is synthetic.

## The story in four sentences

Digital payments are how Indian commerce runs, and payment fraud runs on the same rails. When stolen money lands
in an account, the bank's usual answer is to freeze the whole account, including the kirana shop that sold
groceries to a mule and the payment aggregator that settles other merchants. Wapsi reads the bank's own
transaction data and proposes a better action for each account: hold only the victim's money, leave the rest of
the business running, and prepare the money's return. The bank keeps its merchants and customers, and the victim
gets a restoration packet instead of a grievance number.

## The brief, line by line

| What the track asks | How Wapsi answers it | Evidence in this repository |
|---|---|---|
| "Help a business understand its customers and take better action." | The business is a bank and the merchants it serves. Its customers are the account holders on a fraud trail. The better action is an amount-specific hold in place of a whole-account freeze. | [`README.md`](../README.md), [`docs/overview.md`](../docs/overview.md), [`docs/algorithms/hold-plan.md`](../docs/algorithms/hold-plan.md) |
| Business data: "transactions ... support conversations" | **Transactions:** the ledger (UPI / IMPS transfers, balances). **Support conversations:** NCRP / 1930 complaints and Hinglish voice notes. **Merchant records:** invoices with delivery status and GST registrations decide the merchant rule. | [`docs/data-contract.md`](../docs/data-contract.md), `Transaction`, `Complaint`, `Invoice`, `GstRecord` in [`src/wapsi/models.py`](../src/wapsi/models.py), [`src/wapsi/intake/`](../src/wapsi/intake/) |
| "helps a business attract, serve, convert, or retain customers" | **Serve:** the victim gets a traced, per-victim amount and a restoration packet. **Retain:** the honest merchant and the honest customer keep a working account, so the bank keeps them. | Showcase case: [`src/wapsi/fixtures/showcase.py`](../src/wapsi/fixtures/showcase.py); kirana shop balance ₹51,200 and landlord balance ₹1,55,000 are not frozen |
| Demo: "show how your product turns customer or business data into a useful recommendation or action" | One complaint and its transactions go in; a hold plan with one decision per account comes out, each with its reason and its SOP citation, followed by the drafted instructions. | `uv run python .claude/skills/lien-calculator/scripts/hold_plan.py 31009260000101 --world showcase-memory`; the **Hold Plan** page of `uv run streamlit run ui/app.py`; [`amd/VIDEO_SCRIPT.md`](amd/VIDEO_SCRIPT.md) beat 3 |
| "If your product predicts ... customer behaviour, explain how the prediction was calculated." | The prediction is each account's role: mule, innocent holder, merchant or pool account. It comes from hard rules plus a transparent logistic scorer with 27 hand-set terms over 33 behavioural features; every score is shown as per-feature contributions with plain-English reasons. | [`docs/algorithms/classification.md`](../docs/algorithms/classification.md), [`src/wapsi/classify/`](../src/wapsi/classify/), the Case Room inspector in the app |
| "Do not use a language model as the only forecasting method." | No language model takes part in the prediction or in any amount. `src/wapsi/classify/` and `src/wapsi/graph/` do not import or call one. The second scorer is a GraphSAGE GNN of 78k parameters in pure PyTorch, with the heuristic as fallback. The language model writes prose only. | [`docs/adr/0003-numbers-from-data-words-from-llm.md`](../docs/adr/0003-numbers-from-data-words-from-llm.md), [`docs/adr/0006-transparent-heuristic-classifier-default.md`](../docs/adr/0006-transparent-heuristic-classifier-default.md), [`editions/amd/gnn/README.md`](../editions/amd/gnn/README.md) |

## "Good projects will show"

### 1. A real customer or merchant problem

- Official figures: ₹7,647 Cr held and ₹167 Cr restored, 2.18% of the money held, with 32.80 lakh complaints and
  1,586 entities on CFCFRMS ([`data/docs/facts.json`](../data/docs/facts.json), verbatim quotes with page
  numbers).
- The merchant's side, in the showcase case: the kirana shop sold ₹1,200 of groceries to a mule and would have
  kirana shop balance ₹51,200 frozen. A payment-aggregator pool would have ₹5,00,20,000 frozen. In all, 99.75% of
  what the freeze locks belongs to people who did nothing wrong.
- The rule already exists. The SOP tells banks to hold the disputed amount, and to hold nothing from a merchant
  that delivered its goods (SOP 9.2, citation ids in [`src/wapsi/citations.py`](../src/wapsi/citations.py)). What
  is missing is the data work, which is what Wapsi does.

### 2. A useful commercial action

| Account | Action Wapsi proposes | Rule |
|---|---|---|
| Mule account | hold `min(victim money in the account, balance)` | SOP 5(ix), 9.1 |
| Innocent holder (rent, salary, a payment) | hold the amount, no escalation, EDD memo | SOP 5(xii) |
| Merchant, goods delivered | no hold; update CFCFRMS only; **the merchant keeps trading** | SOP 9.2 |
| Merchant, goods not delivered | hold in the pool account | SOP 9.2 |
| Nodal / pool / escrow account | hold the disputed amount only; the account keeps running | SOP 9.1(iii) |
| Money already at another bank | request to that bank through CFCFRMS | SOP 9.1 |
| The victim | restoration packet, Process 1 or 2 | the SOP's two restoration processes |

Showcase result: ₹1,42,848 held in 5 accounts instead of ₹5,03,50,000 in 6 accounts, and ₹3,34,848 recoverable
for the victim. Code: [`src/wapsi/graph/holdplan.py`](../src/wapsi/graph/holdplan.py). Every action waits for a
supervisor ([`src/wapsi/pipeline/run.py`](../src/wapsi/pipeline/run.py), `approve`).

### 3. Relevant recommendations

- Each recommendation is about one account and carries its reasons (for a mule: how much of the money received
  was sent on, and how fast) and its citation ids. Unclear accounts are not guessed: on the hard world 3.5% of
  accounts are sent to the officer as UNKNOWN.
- Recommendations are checked before anyone sees them: a separate verifier recomputes every amount, account
  number, UTR, date, citation and policy rule ([`src/wapsi/verify/`](../src/wapsi/verify/)).
- Text from a customer cannot change a recommendation: complaint narratives and voice notes never feed tracing,
  classification or the hold plan, and instruction-like text is quoted to the officer
  ([`docs/trust-and-safety.md`](../docs/trust-and-safety.md)).

### 4. A measurable effect

| Effect the track names | Measure | Demo world (39 cases) | Hard world (64 cases) | Source |
|---|---|---|---|---|
| Revenue, working capital | money a freeze would lock that stays free | ₹34.97 Cr of non-victim money left free; ₹29.19 L held against ₹35.26 Cr (0.83% of the freeze) | ₹70.73 L held against ₹39.55 Cr (1.79% of the freeze) | `baseline` in the reports |
| Retention | honest accounts wrongly frozen | 0 false freezes | 0 false freezes | `metrics.false_freeze_count` |
| Retention | accounts a freeze would lock that get no hold at all | 17 | 14 | `baseline` in the reports |
| Retention | honest merchants and customers (merchants and innocent holders): money a freeze would lock against what Wapsi holds | ₹82.99 L would be frozen; Wapsi holds ₹2.50 L (3.0%) | not split | `baseline.by_group` in `demo-baseline-split.json` |

**Where the headline freeze figure comes from, said once.** On the demo world, 96.9% of the freeze baseline sits in
the three nodal / pool accounts, which SOP 9.1(iii) already says must not be frozen whole (hold the disputed amount
only). A domain reader should weigh the headline 0.83% with that in mind: without the pool accounts the freeze is
₹1.10 Cr against ₹27.48 L held (25.0%), and the retention line above is the effect on the bank's own merchants and
customers.
| Service quality | time from complaint to hold plan, p95 | p95 0.35 s | p95 0.50 s | `metrics.p95_time_to_hold_plan_s` |
| Service quality | holds exact against an independent reference | 1.0, over-hold ₹0 | 1.0, over-hold ₹0 | `metrics.hold_exact_match_rate` |
| Restoration | restoration process (P1 or P2) chosen correctly | 1.0 | reported in `hard.json` | `metrics.process_accuracy` |

Reports: [`evals/reports/demo.json`](../evals/reports/demo.json),
[`evals/reports/hard.json`](../evals/reports/hard.json). Timings are from a laptop with the stub model; the hold
plan itself involves no language model. With Gemma 4 on AMD writing the prose, the end-to-end figure is
TBD(tf_demo_p95). These are plans measured against a reference on synthetic data, not money restored at a real
bank; [`docs/limitations-and-roadmap.md`](../docs/limitations-and-roadmap.md) says what would have to be true in
production.

## The technology requirement

"A meaningful part of the workload must run on AMD", and the AMD component must be part of the working product
shown to judges.

| Workload in the product | Runs on | Status |
|---|---|---|
| Prose fields of every draft | Gemma 4 31B on AMD GPUs through the AMD Token Factory endpoint | built during the event and run live on 2026-10-14: the showcase world with Gemma 4 writing the prose, drafts passing the verifier 1.0; the public demo replays 5 prose fields recorded from Gemma 4 on AMD (AMD Token Factory, 2026-10-14), labelled as a recording. Demo world: TBD(tf_demo_pass) |
| Complaint and voice-note field extraction, translation | the same model and endpoint | built during the event and run live on 2026-10-14 on the six sample transcripts: 98.3% of the extracted values agree with the expected answers (AMD Token Factory, text model only); with Whisper transcribing: TBD(voice_asr_samples) |
| The officer's agent and its tool calls (Google ADK) | the same model and endpoint | built during the event and run live on 2026-10-14: 5 tool calls behind "Open case 31009260000101" (AMD Token Factory, Gemma-4-31B-IT); that live run ended VERIFIED: 16 of 16 drafts verified, with Gemma 4 on AMD writing the draft prose too |
| GraphSAGE mule scorer (optional; the transparent scorer is the default) | built to run on ROCm: AMD GPU notebook or MI300X droplet | prepared and CPU-tested; not yet run on AMD. AMD GPU training TBD(gnn_amd_auc) |
| Dedicated serving of both models next to the GNN | vLLM on one MI300X (droplet mode) | compose stack and benchmarks prepared and tested offline; TBD(droplet_tokens_s) |

The endpoint is a setting (`WAPSI_LLM_BASE_URL`, `WAPSI_LLM_API_KEY`, `WAPSI_LLM_MODEL`), so the same build runs
against AMD's shared API, a dedicated AMD endpoint or a droplet. Modes and honest status:
[`docs/editions.md`](../docs/editions.md).

## The judging criteria

| Criterion | Where to look |
|---|---|
| Application of Technology | [`docs/editions.md`](../docs/editions.md) (what runs on AMD), [`GOOGLE_PRIZE.md`](GOOGLE_PRIZE.md) (Gemma 4 and ADK), [`docs/architecture.md`](../docs/architecture.md) |
| Presentation | [`amd/DECK_OUTLINE.md`](amd/DECK_OUTLINE.md), [`amd/VIDEO_SCRIPT.md`](amd/VIDEO_SCRIPT.md), [`START_HERE_FOR_JUDGES.md`](START_HERE_FOR_JUDGES.md) |
| Business Value | sections 1, 2 and 4 above; deck slides 7 and 11 |
| Originality | [`docs/originality.md`](../docs/originality.md): the tag `amd-baseline` and what was built during the event |

## The submission checklist

| The form asks for | Where it is |
|---|---|
| Title, short and long description, selected challenge, technology tags, cover-image brief | [`amd/LABLAB_FIELDS.md`](amd/LABLAB_FIELDS.md) |
| Working prototype | `uv run streamlit run ui/app.py`; the hosted URL goes into `LABLAB_FIELDS.md` |
| Demo video | [`amd/VIDEO_SCRIPT.md`](amd/VIDEO_SCRIPT.md) |
| Pitch presentation | [`amd/DECK_OUTLINE.md`](amd/DECK_OUTLINE.md) |
| Public GitHub repository, MIT licence, original work | [`LICENSE`](../LICENSE), [`docs/originality.md`](../docs/originality.md) |
| Demo application platform and URL | a line in `LABLAB_FIELDS.md`, filled on submission day |
