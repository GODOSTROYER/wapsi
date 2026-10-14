# AMD ACT III video: 4:15 (limit 5:00)

**Challenge: Track 3, Reinvent Commerce.** The track's demo brief is: show how the product turns customer or
business data into a useful recommendation or action, and explain how any prediction is calculated. The cut does
exactly that, in that order, and shows the AMD workload inside the working product.

**Screen:** the Wapsi officer app (Inbox, Case Room, Hold Plan, Drafts & Verification, Voice Intake, Agent trace,
Approvals, Audit) running against Gemma 4 on the AMD endpoint, plus four slides. **Voice:** one narrator, about
590 words. Every number is a row in [`../NUMBERS.md`](../NUMBERS.md). A figure written `TBD(slot_id)` is an open
slot: read it from its results file on the day and register it in `NUMBERS.md` before recording.

**Capture rules**
- Record between 12 and 18 Oct. Show the model name on a draft (`Gemma-4-31B-IT`) and the endpoint host once.
- Keep the API key, tokens and IP addresses off screen. Never open the `.env` file on camera.
- If a slot is still open on the day, cut its sentence. Do not say "about" and do not estimate.
- Say where a number ran: "on AMD's Token Factory", "on an AMD GPU notebook", "on an MI300X". Never call a Token
  Factory figure an MI300X benchmark.
- All data on screen is synthetic; the first frame of the app says so.

---

## 0:00 to 0:20. The shop that was frozen

**Screen:** one line of text, "A kirana shop sold groceries. Its whole account was frozen." Then the baseline
strip: `Whole-account freeze: ₹35.26 Cr · 101 accounts` / `Wapsi: ₹29.19 L · 84 accounts` / `0 false freezes`.

**VO:** "A kirana shop sells twelve hundred rupees of groceries. The buyer was spending stolen money, so the bank
freezes the shop's whole account. On our 39-case test world, freezes like that lock ₹35.26 crore. Wapsi holds
₹29.19 lakh: only the victim's money, with zero false freezes."

## 0:20 to 0:45. A commerce problem and a data problem

**Screen:** slide: `₹7,647 Cr held · ₹167 Cr restored · 2.18%`, then three icons: merchant, customer, bank.

**VO:** "India held ₹7,647 crore for cyber-fraud victims and returned ₹167 crore: 2.18%. Money goes back only
when someone proves whose money sits in which account. Freezing everything skips that proof, and the people who
pay are honest merchants and customers. The bank loses them, and answers the grievance. That is a commerce
problem, and it is a data problem."

## 0:45 to 1:35. Data in, action out: one case

**Screen:** Inbox, then showcase case `31009260000101` in the Case Room: the trail graph, layer by layer. Then the
Hold Plan with the baseline strip, hovering on the kirana shop, the landlord and the pool account. Then Drafts &
Verification with every check green.

**VO:** "Here is the bank's own data: one complaint and its transactions. ₹4,80,000 stolen, moved through four
layers of accounts in ninety minutes. Wapsi traces every hop and splits mixed money per victim, the way the
government's procedure prescribes. Then the action. The kirana shop delivered its goods: no hold, it keeps
trading. The landlord who was paid rent: ₹4,800 held, not his whole account. The payment aggregator's pool:
₹19,200 held out of five crore, so every other merchant is still settled. In total ₹1,42,848 is held instead of
₹5,03,50,000, and ₹3,34,848 is recoverable for the victim. The hold instructions, the notices and the
suspicious-transaction report are already drafted."

## 1:35 to 2:05. How the prediction is calculated

**Screen:** Case Room inspector on the first mule and on the landlord: the score, the per-feature contributions
and the plain-English reasons. Then deck slide 6 (the scorer and the GNN table).

**VO:** "Which account is a mule and which is an honest shop is a prediction, and no language model makes it. A
transparent scorer weighs 33 behavioural features, such as how fast the money was passed on, and shows its
reasons in plain English. An optional graph neural network, trained on an AMD MI300X, is a second opinion.
Unclear accounts go to the officer. And the amount held is never predicted: it is arithmetic."

*The hard-world figures of the GNN trained on the MI300X are on deck slide 6; the voice-over does not read them.*

## 2:05 to 2:50. Gemma 4 on AMD, a voice note and the agent trace

**Screen:** a draft with the model name `Gemma-4-31B-IT`. Then Voice Intake: the sample **Fatima Shaikh · courier
scam · embedded prompt injection**, the transcript and translation, the reconciliation table, the warning
"Instruction-like text, not followed." Then the Case Room's **Agent trace** tab: numbered tool calls with their
checks.

**VO:** "The words come from Gemma 4, Google's open model, served on AMD GPUs. It writes the sentences in each
draft around placeholders; every amount, account and date is filled from data, and a verifier recomputes all of
them. It also reads complaints. This victim's Hinglish voice note becomes a transcript, and Gemma extracts the
fields. Every number she says is checked against the ledger. The note hides an order to release the hold: it
is quoted to the officer, and the hold stays. The officer's agent runs on Google's Agent Development Kit, and
every tool call it makes is recorded here, with its checks."

*Say "transcribed with Whisper" only if this beat is recorded from `editions/amd/app/ui_app.py` with
`WAPSI_VOICE_BACKEND=open WAPSI_ASR_BACKEND=local` and the Whisper weights downloaded beforehand, so the transcript on
screen really came from Whisper. With the bundled samples' scripts standing in for the recogniser, keep the line as
written.*

## 2:50 to 3:25. Measured on AMD

**Screen:** deck slide 8's MI300X results, then slide 10's raw-model comparison. Caption on screen: "Measured on
an AMD Instinct MI300X (AMD AI Notebook, ROCm 7.14, vLLM), 14-10-2026".

**VO:** "Measured with Gemma 4 served on an AMD MI300X: on all 39 test cases every draft passes the verifier, and
every hold is exact. One MI300X runs 1,590 complete cases an hour. Asked for the plan in free text, the raw model
gets an amount wrong in 19 of 39 cases. Inside Wapsi, 0 of 301 drafts carry a wrong amount. Even when
we force a model to invent a number in every paragraph, 27.57% of drafts get their prose replaced, and every
draft still passes."

## 3:25 to 3:50. Why a bank can trust it

**Screen:** Approvals as an analyst (no approve button), then as a supervisor (approve, fingerprint shown). The
Audit page, then the tamper drill output. Then the hard-world row of deck slide 6.

**VO:** "Nothing leaves without a supervisor, and every step is sealed in a tamper-evident log. We also built a
harder world to fool the classifier. There, 92% of dispositions are right, and every miss is a label, never
money: holds stay exact, with zero false freezes."

## 3:50 to 4:15. Business value and the ask

**Screen:** slide: `1,586 entities on CFCFRMS · 32.80 lakh complaints`, the line "MuleHunter flags mules; Wapsi
returns the money", then the title slide.

**VO:** "1,586 banks and payment companies receive these hold requests, across more than 32 lakh complaints.
Tools like MuleHunter flag mule accounts. Wapsi does what comes after: hold the right amount, return the money,
and keep honest businesses running, on an open model and an AMD endpoint the bank controls. Wapsi: hold the
fraud, not the shop."

---

## Timing check

| Segment | Start | Seconds |
|---|---|---:|
| The shop that was frozen | 0:00 | 20 |
| A commerce problem and a data problem | 0:20 | 25 |
| Data in, action out | 0:45 | 50 |
| How the prediction is calculated | 1:35 | 30 |
| Gemma 4 on AMD, voice note, agent trace | 2:05 | 45 |
| Measured on AMD | 2:50 | 35 |
| Why a bank can trust it | 3:25 | 25 |
| Business value and the ask | 3:50 | 25 |
| **Total** | | **255 s = 4:15** |

- The limit is 5:00. The 45 seconds of slack are for a slower read, not for more content.
- "Twelve hundred rupees" is the kirana shop's sale (the kirana shop sold ₹1,200 of groceries to a mule). "Five
  crore" is the spoken form of the pool account balance ₹5,00,20,000. "More than 32 lakh complaints" is the
  spoken form of 32.80 lakh complaints (to 30.06.2026). "92%" is the spoken form of dispositions 0.9198.
- "Every draft passes the verifier" is drafts passing the verifier 1.0 and "every hold is exact" is holds exact
  1.0 on the 39-case demo world, Gemma 4 31B served by vLLM on the MI300X; "19 of 39" and "0 of 301" are the same
  run's raw-model comparison (`golden_eval.json`, `ab_test`). "1,590 complete cases an hour" is the end-to-end run
  at concurrency 16 (`bench/e2e_c16.json`). All of these are MI300X notebook figures, never Token Factory ones. The
  golden-set figures and the throughput come from two notebook sessions on two different MI300X GPUs
  (`editions/amd/evidence/notebook/2026-10-14/SESSIONS.md`), so the VO says "an MI300X" and "one MI300X", never
  "the same GPU". In the throughput run 9 of 83 model-written prose fields were replaced by fixed text (0.1084) and
  every draft still passed; the VO does not claim the model's prose always survived.
