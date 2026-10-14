# Draft contract (shared by `wapsi.draft` and `wapsi.verify`)

A `Draft` is rendered from a Jinja template. Every number, account id, UTR and date in `Draft.body` comes from
`Draft.slots`, and every slot is re-derivable from the `CaseBundle`. The verifier recomputes each slot with the
resolver below and requires an exact match of the rendered string.

## Rendering
- Amounts: `wapsi.money.fmt_inr(x)` → `₹4,80,000.00` (always with paise).
- Dates: `wapsi.timeutil.fmt_ist(ts, with_time=False)` → `10-09-2026`; datetimes `fmt_ist(ts)` → `10-09-2026 11:02 IST`.
- Lists: `", "`-joined, sorted ascending.

## Skeleton (positional binding)
Every draft produced by `wapsi.draft` carries `structured["skeleton"] = {"title": ..., "body": ...}`: the title and
body with `⟦slot_key⟧` wherever a slot value was inserted, whether by the template, by LLM prose (its accepted
placeholder text, or the fallback paragraph) or by cleaned free text (an amount in a classifier reason that equals
a slot becomes that slot). `title` and `body` are exactly the skeleton with each `⟦key⟧` replaced by
`slots[key]`.
- The drafter renders templates with private-use markers in place of slot values and turns them into
  `⟦key⟧`; any literal `⟦`/`⟧` in data (names, labels, remarks) becomes `(`/`)`, and forged markers are dropped,
  so data can never create a slot reference. Bodies therefore never contain `⟦` or `⟧`.
- The verifier fills the skeleton with its **own recomputed** values (the resolvers below) and requires exact
  equality with `draft.title` / `draft.body`. A difference is the error `body_skeleton_mismatch` (spans: the
  first differing stretch of the draft), so a correct value in the wrong place (hold and disputed amounts
  swapped, a reordered UTR list, an edited title) fails. A skeleton key that cannot be recomputed is
  `skeleton_unresolvable`; a skeleton without string `title` and `body` is `body_skeleton_malformed`.
- No skeleton is the error `body_skeleton_missing`, unless the draft is hand-written:
  `structured["hand_written"] is True` and no `skeleton` key. Only then does the verifier fall back to the
  any-slot checks below alone. The flag never excuses a draft that has a skeleton.

## Slot keys and resolvers
`acct` = `draft.account_id` (may be None for case-level drafts). `dec` = plan decision for `acct`.
`st` = `trace.accounts[acct]`. `plan` = `bundle.plan`. `trace` = `bundle.trace`.

| Slot | Resolver |
|---|---|
| `account_id` | `acct` |
| `bank_id` | `st.bank_id` |
| `disposition` | `dec.disposition` (enum value) |
| `hold_amount` | `fmt_inr(dec.hold_amount)` |
| `forward_hold_amount` | `fmt_inr(dec.forward_hold_amount)` |
| `tainted_amount` | `fmt_inr(dec.tainted_amount)` |
| `balance_now` | `fmt_inr(dec.balance_now)` |
| `disputed_amount` | `fmt_inr(st.tainted_received_total)` |
| `utrs` | UTRs of `trace.edges` with `to_account == acct` and `tainted_total > 0`, sorted, joined |
| `inflow_dates` | unique `fmt_ist(e.ts, with_time=False)` of those edges, sorted chronologically, joined |
| `victim_amount:<ncrp_id>` | `fmt_inr(dec.per_victim[ncrp_id])` if `acct` else `fmt_inr(plan.per_victim_recoverable[ncrp_id])` |
| `ncrp_ids` | `bundle.case.ncrp_ids` sorted, joined |
| `total_stolen` | `fmt_inr(trace.totals.stolen)` |
| `total_cashed_out` | `fmt_inr(trace.totals.cashed_out)` |
| `total_outbound` | `fmt_inr(trace.totals.outbound)` |
| `total_hold` | `fmt_inr(plan.total_hold)` |
| `total_forward_hold` | `fmt_inr(plan.total_forward_hold)` |
| `total_recoverable` | `fmt_inr(plan.total_recoverable)` |
| `baseline_freeze_amount` | `fmt_inr(plan.baseline_freeze_amount)` |
| `restoration_process` | `plan.restoration_process` (enum value) |
| `str_ground` | `plan.str_ground` (enum value) or empty string |
| `complaint_amount:<ncrp_id>` | `fmt_inr(complaint.amount_claimed)` |
| `reported_at:<ncrp_id>` | `fmt_ist(complaint.reported_at)` |
| `holder_name` | customer name of `acct` from the ledger (verifier checks against the ledger) |
| `victim_account:<ncrp_id>` | `complaint.victim_account` |
| `edge_amount:<txn_id>` | `fmt_inr(edge.amount)` for the trail edge with that `txn_id` |
| `edge_tainted:<txn_id>` | `fmt_inr(edge.tainted_total)` |
| `edge_ts:<txn_id>` | `fmt_ist(edge.ts)` (with time) |
| `edge_utr:<txn_id>` | `edge.utr` |
| `<account_id>/<key>` | any account-scoped key above resolved with `acct = <account_id>` (used by case-level drafts: CFCFRMS table, STR sheets, attribution sheet); the account must be in `trace.accounts` |

Unknown slot keys are a verification **warning**; a slot value that does not match its resolver is an **error**.

## Body rules (checked by the verifier)
Token shapes live in `wapsi.money` and are shared by the drafter (which sanitises free text with them) and the
verifier. All of them work on `money.normalize_text`, which folds every Unicode digit (Devanagari `०-९`,
full-width `０-９`, ...), full-width ASCII, dashes and non-ASCII spaces to ASCII without moving offsets.

- **Title and body equal the filled skeleton** (see "Skeleton" above).
- **Money** (`money.find_money_mentions`; `find_amounts` returns the same as `(text, value)` pairs). An amount
  phrase is a digit number (`4,80,000.00`, `480,000`, `480000`), number words (English `one … ninety-nine`,
  Hinglish `ek, do, teen, char, paanch, …, das`) or both mixed (`9 lakh 50 thousand`), multiplied by scale words
  (`hundred`, `thousand`/`hazaar`/`k`, `lakh`/`lac`, `million`/`mn`, `crore`/`cr`, `billion`/`bn`/`arab`), so
  `₹43,200.00 crore` is 4.32 × 10¹¹ and `₹1.2 lakh` is 1,20,000. The phrase is money when a currency marker is
  next to it (before: `₹ ₨ रु Rs Rs. INR Re. rupee(s) rupaye`, any spacing; after: `rupee(s) rupaye Rs INR /-
  paise`, optionally then `only`), or when it has a scale of a thousand or more, or is a comma-grouped number,
  and is not a count (`1,200 transactions`, `5 lakh accounts`). Digits glued to letters or joined by `- / : .`
  (`HB01`, `TX-001`, `10-09-2026`, `11:02`) are identifiers, not money. Examples that are money:
  `9,99,999 rupees`, `INR  9,99,999.00`, `Rs.4,80,000/-`, `Rs. five lakh only`, `nine lakh fifty thousand
  rupees`, `rupees two crore`, `₹९,९९,९९९`, `480,000`.
  Every money mention in the title/body must equal the amount of a verified slot (`body_amount_unmatched`).
- **Accounts** (`money.find_account_ids`): `XX00-0000000000` in any case, with `-`, a space or nothing between
  the parts (and `XX00-` followed by 8–12 digits), normalised to upper case; each must be `acct`, an account in
  `trace.accounts` or a complaint victim account (`unknown_account`).
- **UTR-like references** (`money.find_utrs`): 12-digit RRNs, also written in groups (`4253 1000 0001`,
  `4253-1000-0001`), and alphanumeric tokens of 12–22 characters with at least 9 digits and at most 8 letters
  (NEFT / RTGS UTRs such as `HB01N26026123456`, `N260261234567890`, `HDFCR52024091012345678`; `ATM…`, `CSH…`),
  normalised to upper case without separators. Each must be the UTR or transaction id of a `trace.edges` entry
  (`unknown_utr`).
- **Dates** (`money.find_dates`): `10-09-2026`, `10/09/2026`, `10.09.2026` (day first), `2026-09-10` (ISO; a
  timestamp with a zone is converted to IST), `10 Sep 2026`, `10th of September, 2026`, `September 10, 2026`.
  Each must exist and be a case date (case / trace / edge / complaint / draft / approval / action / clock
  timestamps, as IST dates) or a date written in the title of a source the draft cites (the FIU-IND circular of
  `02.06.2025` when `FIU-CIRC-2025-06-02` is cited) (`unknown_date`).
- **Structured payload** (everything except `skeleton` and the attached `citations` quotes): account, UTR and
  date checks as above on every string, dict key and integer (dates may also be account-opening dates and
  holder dates of birth from the ledger); money mentions in strings, and every number stored under a money key
  (`amount`, `*_amount`, `amount_*`, `balance(_now)`, `tainted*`, `total_*`, `stolen`, `hold`, `held`,
  `outbound`, `cashed_out`, `recoverable`, ... or any value inside `per_victim`, `per_victim_recoverable`,
  `tainted_by_complaint`, `forward_holds`, `totals`; not under `timelines_days`, `timeline`, `signals`,
  `network`), must equal a verified slot amount or an amount of the bundle (trace totals, edge and account
  amounts, decision and plan amounts, complaint claims) (`structured_amount_unmatched`).
- Citations appear as `[SOP-9-1]`-style tokens; each must be in `wapsi.citations.KNOWN_IDS` and in
  `draft.citation_ids`.
- `structured` may carry `llm_fallback` (bool), `llm_attempts` (int) and `llm_issues` (list of issue codes,
  never untrusted text) for each LLM prose field, and `drafter_warnings` (e.g. transaction remarks / labels
  could not be loaded, so they were left out).
- **LLM prose** is generated with placeholders `⟦slot_name⟧` that the drafter substitutes; the LLM never writes
  values directly. Before substitution the drafter rejects (retry once with feedback, then a deterministic
  paragraph): unknown or broken placeholders, template syntax, citation tokens, copied complaint text,
  - any digit, except in a legal reference: `Section / Sections / Sec. / s. / ss. / Rule / Para` followed by
    numbers of at most three digits (dotted sub-numbers and bracketed sub-clauses allowed: `Section 106`,
    `Sections 94 and 168`, `Rule 2(1)(g)`, `Para 9.1(iii)`); never a four-digit or longer number, never a year
    (`raw_digits`);
  - any currency word or symbol: `rupee(s)`, `rupaye`, `Rs`, `INR`, `Re.`, `paise`, `₹`, `₨`, `रु`, `/-`
    (`currency_words`);
  - any number word (English or Hinglish, see above) unless it is a small count: value at most ninety-nine, no
    scale word (`hundred`, `thousand`, `lakh`, `crore`, …), not within two words of a currency word, and
    followed (directly or after at most three filler words such as `of the`, `mule`, `earlier`) by a count noun
    (`accounts`, `layers`, `victims`, `days`, `hours`, `minutes`, `transfers`, `complaints`, `transactions`, …;
    `wapsi.money.COUNT_NOUNS`). Hinglish words that are also English words or names (`do`, `das`, `teen`,
    `char`, …) count only inside a longer number or next to a currency word (`number_words`);
  - money mentions, account numbers and UTRs (any spelling) that do not match an offered placeholder value;
  - an answer that is not language at all (`not_prose`), and a sentence that instructs a whole-account or debit
    freeze, a block, the suspension of digital banking or a seizure (`freeze_wording`: the verifier's policy
    wording rule, applied to the prose before it is accepted).
  The verifier's money and policy checks apply to whatever gets through.

## Draft kinds per decision
| Decision / condition | Drafts |
|---|---|
| `HOLD_AMOUNT`, `HOLD_AMOUNT_NO_ESCALATION`, `HOLD_IN_POOL`, `HOLD_AMOUNT_INTERMEDIARY`, `FORWARD_HOLD` | `HOLD_INSTRUCTION` (+ `CUSTOMER_NOTICE`, addressed to the intermediary for pool/nodal/escrow accounts) |
| `HOLD_AMOUNT_NO_ESCALATION` | `EDD_MEMO`. For an unwitting conduit (reason code `STR_RECOMMENDED_OFFICER_DECISION`) the memo sets `str_recommended = True` and `str_decision = "OFFICER"`, and the hold instruction and CFCFRMS update recommend an STR for the officer's decision; a bona fide recipient's memo recommends none (`bona_fide_str_recommended`, `edd_conduit_str_missing`) |
| `EXCEPTIONAL_ACCOUNT_LEVEL_HOLD` (after a supervisor approved the officer's request) | `HOLD_INSTRUCTION` redrafted as a new version stating the recorded reasons and the approval; the case's `CFCFRMS_UPDATE` is redrafted too; the account's unapproved `CUSTOMER_NOTICE` and `EDD_MEMO` are withdrawn. Only this hold instruction may instruct account-level action (`whole_account_freeze` otherwise) |
| an officer's request for an exceptional account-level hold (`request_account_level_hold`) | `ACCOUNT_LEVEL_HOLD_REQUEST` (`account_level_request.j2`): `structured` carries `requested_by`, the recorded `reasons` from the closed `AccountLevelReason` list and the amount-specific hold; cites `SOP-5-IX`, `SOP-9-1`. The verifier checks the reasons, the eligibility and that the requester is not the approver |
| `OUTBOUND_CFCFRMS_REQUEST` | `OUTBOUND_REQUEST` |
| case with any `MULE` role holding tainted money or cash-outs | one case-level `STR` (`structured` = FINnet-style sheets; `account_id` None). Innocent holders (classification or decision role `BONA_FIDE_RECIPIENT` / `INNOCENT_HOLDER` or `UNWITTING_CONDUIT`, or `HOLD_AMOUNT_NO_ESCALATION`) at **any** bank are never identified: there is no automatic STR on them; not in the body, the sheets, the beneficiary list, the network section or the slot keys; the STR describes them as "account operated without the holder's knowledge, not identified" and states their amounts with `edge_tainted:<txn_id>` slots. The verifier fails `innocent_in_str` if an innocent holder's account number (any spelling) or name appears in the title, body, structured payload or slots. |
| every case | one `CFCFRMS_UPDATE`, one `RESTORATION_PACK` (P1 or P2) |
| `NO_HOLD_UPDATE_ONLY` | no account draft; listed in `CFCFRMS_UPDATE` |

`CFCFRMS_UPDATE` and `OUTBOUND_REQUEST` drafts carry the "Simulated CFCFRMS interface" disclaimer, and the
`CFCFRMS_UPDATE` actions created on approval carry `payload.interface = "SIMULATED_CFCFRMS"`.

The reply to a police / LEA hold request is not a `Draft` but follows the same contract:
`wapsi.verify.lea_request.check_lea_request` renders `lea_clarification.j2`, and every amount, account number,
UTR and date in the letter comes from the request or the case data; the request's own text is quoted back as
untrusted, never copied into the letter's instructions.
