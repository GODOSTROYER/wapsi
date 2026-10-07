# Glossary

Short definitions of the Indian legal, banking and payments terms used in Wapsi, and of Wapsi's own terms. Where a
term maps to something in the code, the entry names it. The legal rules behind these terms are on the
[legal spine](legal-spine.md) page.

SOP means the MHA / I4C *Standard Operating Procedure for NCRP-CFCFRMS, Custody, Restoration of money and
Grievance Redressal* (02.01.2026). "SOP 12(x)" means Para 12, clause (x).

## Institutions and systems

| Term | Meaning |
|---|---|
| **1930** | India's national cyber-fraud helpline number. A call to 1930 registers a complaint in the same system as NCRP (SOP 12(v), 12(vii)). In Wapsi, `Complaint.source` is `"NCRP"` or `"1930"`. |
| **CFCFRMS** | Citizen Financial Cyber Fraud Reporting and Management System. The I4C platform that pushes complaints to banks and financial intermediaries and records their holds and the onward money trail (SOP 6). Wapsi is not connected to it: it is a **simulated CFCFRMS interface**, and every CFCFRMS action it prepares is labelled so (`payload.interface = "SIMULATED_CFCFRMS"`). |
| **DoT FRI** | Financial Fraud Risk Indicator from the Department of Telecommunications: a risk rating of a mobile number linked to financial fraud, shared with banks and payment providers. Wapsi stores a synthetic per-account value in `ExternalSignal.fri_risk` (`NONE`, `MEDIUM`, `HIGH`, `VERY_HIGH`) and uses it as a classifier feature. |
| **FINnet 2.0** | FIU-IND's reporting system, through which reporting entities file STRs and other reports. Wapsi's STR draft is laid out like FINnet sheets (`"FINnet 2.0 (modelled)"`), but it is not a file you can upload. |
| **FIU-IND** | Financial Intelligence Unit – India, Ministry of Finance. The national agency that receives STRs under the PML Rules. |
| **Grievance Redressal Module** | The part of NCRP-CFCFRMS where a bank submits an account holder's grievance against a hold, and where the IO and the District Grievance Officer record decisions (SOP 10). |
| **I4C** | Indian Cyber Crime Coordination Centre, under MHA. It issued the SOP and runs NCRP and CFCFRMS. |
| **MHA** | Ministry of Home Affairs, Government of India. |
| **MRM** | Money Restoration Module of NCRP-CFCFRMS. A victim applies there for interim custody of money on hold, and IOs record their attribution of it (SOP 11, Processes 1 and 2). |
| **MuleHunter.AI** | A mule-account detection model built by the Reserve Bank Innovation Hub for banks. Wapsi stores a synthetic score in `ExternalSignal.mulehunter_score`. It is one of the classifier's input features, not a hard rule. |
| **NCRP** | National Cybercrime Reporting Portal, cybercrime.gov.in. Each complaint gets a 14-digit acknowledgement number (SOP 6.1(d)). Wapsi's synthetic complaints also have 14-digit ids (`ncrp_id`). |
| **NPCI** | National Payments Corporation of India. It operates UPI and IMPS. |
| **RBI** | Reserve Bank of India, the banking regulator. Its (Commercial Banks – Fraud Risk Management) Directions, 2026 (RBI/DoS/2026-27/412, 31.07.2026) apply to Commercial Banks only and are in Wapsi's citation registry as `RBI-FRM-DIR-2026` (Para 24, money mule accounts) and `RBI-FRM-DIR-2026-EWS` (Para 12, early warning signals). Their Para 67 repeals the earlier fraud-risk directions for Commercial Banks; RBI's list of withdrawn circulars names the 2024 Master Directions, whose id `RBI-FRM-MD-2024` is retired. No draft cites the RBI ids yet. |
| **RBI draft SOP on money-mule accounts** | Annex III of the draft KYC Amendment Directions, 2026, issued by the RBI on 11 Sep 2026 for comments (comments close 2 Oct 2026; proposed effective 1 Apr 2027). A **DRAFT**: Wapsi tracks it on the regulatory radar, labels it DRAFT and never treats it as in force. |

## Law and procedure

| Term | Meaning |
|---|---|
| **Annexure-I** | The SOP's form of notice under s.168 read with s.94 BNSS. It directs a bank to hold the amount equivalent to the disputed amount received in an account (SOP PDF pp. 61–62; `SOP-ANNEX-I`). |
| **Annexure-II** | The SOP's form of notice under s.106(1) BNSS for seizure of an account or property. Wapsi does not draft it. |
| **Annexure-III** | The SOP's list of relevant legal provisions, court orders and judgments on freezing and restoration, including summaries of *Dr Sajeer* and *Mohammed Saifullah* (`CASE-SAJEER`, `CASE-SAIFULLAH`). |
| **Annexure-V** | The SOP's worked illustrations of interim custody. Illustration 1 attributes money by the order of transactions. Illustration 2 shares it pro-rata. Wapsi reproduces Illustration 2 to the paisa (`tests/test_sop_annexure_v.py`). |
| **BNS** | Bharatiya Nyaya Sanhita, 2023: the criminal code that replaced the Indian Penal Code (IPC). |
| **BNSS** | Bharatiya Nagarik Suraksha Sanhita, 2023: the code of criminal procedure that replaced the Code of Criminal Procedure, 1973 (CrPC). The SOP gives the CrPC equivalent in brackets, for example s.106(3) BNSS (s.102(3) CrPC). |
| **BNSS s.94** | Power to require, by written order, production of a document or other thing. Read with s.168, it is the legal basis of the CFCFRMS hold notice (`BNSS-94`). |
| **BNSS s.106** | Police power to seize property found in suspicious circumstances. s.106(3) is the route for Process 1 and 2 interim custody (`BNSS-106`). |
| **BNSS s.107** | Attachment, forfeiture or restoration of property through the Court or Magistrate. s.107(6) provides for rateable distribution to victims. It is the basis of Process 4 (`BNSS-107`). |
| **BNSS s.168** | Duty of police to prevent cognizable offences. Read with s.94, it is the basis of the hold notice (`BNSS-168`). |
| **BNSS ss.497, 498, 503** | Custody and disposal of property: s.497 by the court pending trial, s.498 at the end of the trial, and s.503 for property seized by the police and reported to a Magistrate. They are the basis of Process 3. |
| **CEFC** | Cyber-enabled financial crime: a financial crime committed by abusing cyberspace (SOP 12(vi)). |
| **Complainant** | A person in India who files a cybercrime complaint on NCRP or through 1930 (SOP 12(v)). In Wapsi, the victim of a `Complaint`. |
| **CrPC** | Code of Criminal Procedure, 1973, now replaced by BNSS. |
| **DGO** | District Grievance Officer: a police officer of Additional SP or Deputy SP rank. A grievance the IO has not decided within 15 days goes to the DGO automatically, and a holder unhappy with the IO's order can ask the DGO for a review (SOP 10, 10.1(f)). Wapsi's `DGO_ESCALATION` clock. |
| **FIR / e-FIR** | First Information Report, registered under s.173 BNSS. The SOP makes seizure orders under s.106 only with respect to an FIR (SOP 5(iv)), and ties Process 1 custody of amounts above ₹50,000 to an FIR (Process 1(b)). Wapsi's s.106 flag needs `Complaint.fir_number`. |
| **Grievance** | An account holder's challenge to a hold. It is raised through the bank branch and submitted by the bank to the Grievance Redressal Module within 7 days of the holder's complaint (SOP 10.1(a)). |
| **Indemnity bond** | The victim's written undertaking, given before receiving interim custody, to produce the amount before the court when required and to follow the court's further orders (SOP Process 1(iv), 2(f)). Part D of Wapsi's restoration pack. |
| **Interim custody** | Release of held or seized money to the victim while the case continues, under one of the SOP's five processes (SOP 11). |
| **IO** | Investigating Officer: the police officer investigating the complaint (SOP 12(xi)). The IO verifies grievances and attributes held money between victims. |
| **LEA** | Law Enforcement Agency: the State or UT police investigating cyber-enabled financial crime (SOP 12(xiii)). |
| **PML Rules** | Prevention of Money-laundering (Maintenance of Records) Rules, 2005. Rule 2(1)(g) defines a suspicious transaction (`PMLR-2-1-G`). Rule 8 covers furnishing reports to FIU-IND (`PMLR-8`). |
| **PMLA** | Prevention of Money Laundering Act, 2002. SOP 9.1 refers banks to its s.12AA on enhanced due diligence. |
| **Principal Officer** | The officer of a reporting entity who furnishes STRs to FIU-IND (PML Rule 8). |
| **Process 1 to 5** | The SOP's five alternative routes to interim custody or restoration (SOP 11). P1: single victim, s.106(3). P2: multiple victims, attribution under Para 5(xiii). P3: court, ss.497/498/503. P4: s.107 attachment and forfeiture. P5: a process a court directs. Wapsi proposes only P1 or P2 (`RestorationProcess`). |
| **Reporting entity** | A bank or other institution that must report to FIU-IND under PMLA. |
| **Seizure of account** | Subjecting a bank account to seizure under s.106 BNSS or another law (SOP 12(xviii)). Wapsi never instructs it. |
| **SHO** | Station House Officer: the officer in charge of a police station. SHOs issue the Annexure-I notice and assign grievances to IOs. |
| **SOP** | The MHA / I4C Standard Operating Procedure of 02.01.2026. Wapsi uses the copy circulated by the High Court of Uttarakhand on 18.06.2026 (`data/docs/MANIFEST.md`). |
| **STR** | Suspicious Transaction Report filed with FIU-IND. The FIU-IND FAQ says within 7 working days of being satisfied that a transaction is suspicious. The consolidated Rule 8(2) says "promptly". Wapsi drafts one per case when a mule still holds victim money or has cashed some out. |
| **Supurdnama / Supurdignama** | A custody undertaking: the person receiving property or money into interim custody (*supurdagi*) promises to produce it before the court when directed. The SOP calls it the certificate of custody (Supurdignama) and has it updated on NCRP-CFCFRMS (Process 1(viii), 2(l)). Part E of Wapsi's restoration pack. |
| **Suspect bank account holder** | A holder of an account in which reported crime proceeds are parked or flowing through (SOP 12(xvii)). |
| **Suspension of digital banking services** | Stopping an account's electronic channels (net and mobile banking, NEFT, RTGS, IMPS, UPI, cards), leaving only physical branch transactions (SOP 9.1, 12(viii)). Only an LEA directs it. Wapsi never instructs it. |
| **Tipping-off** | Telling a customer that a suspicious transaction report is being made or considered. It is prohibited. SOP 9.1(ii), citing an FIU-IND circular of 02.06.2025, says that when a holder approaches the bank, telling her which LEA ordered the action may not be considered a tip-off (`SOP-9-1-II`). Wapsi's customer notice relies on this and never mentions an STR. |

## Banking and payments

| Term | Meaning |
|---|---|
| **AePS** | Aadhaar-enabled Payment System: withdrawals and transfers authenticated with Aadhaar at a banking agent. One of the channels SOP 9.1 lists for suspension. |
| **Beneficiary bank** | The bank that receives funds and makes them available to the beneficiary account holder (SOP 12(iv)). |
| **Bona fide recipient** | An innocent holder that kept the victim money as rent, salary or payment. `Role.BONA_FIDE_RECIPIENT` (wire value `INNOCENT_HOLDER`, alias `Role.INNOCENT_HOLDER`). No STR recommended by default. |
| **CBDC wallet** | A wallet for the RBI's central bank digital currency (e-rupee). SOP 9.3 has its issuer hold the disputed amount (`SOP-9-3`). Not modelled in Wapsi. |
| **CDD** | Customer due diligence: the standard KYC checks a bank runs on a customer. |
| **Crore** | 1,00,00,000 (ten million). ₹5,03,50,000.00 is ₹5.035 crore. |
| **Debit freeze** | A block on all debits from an account. Wapsi drafts never instruct one outside an approved account-level exception: the verifier fails `whole_account_freeze`. |
| **Disputed amount** | The amount reported against a transaction on CFCFRMS. In Wapsi drafts, the `disputed_amount` slot is the victim money the account received (`tainted_received_total`). |
| **EDD** | Enhanced due diligence: deeper checks on a customer or transaction. SOP 9.1 and 10.1(a) require it for reported accounts and before a bank forwards a grievance. Wapsi drafts an EDD memo for every innocent-holder hold (`HOLD_AMOUNT_NO_ESCALATION`). |
| **Escrow account** | An account that holds funds on behalf of parties until conditions are met. Treated like a nodal or pool account (SOP 9.1(iii)). |
| **First-layer account** | "A bank account which receives the de-frauded money directly from the bank account of victim reporting on NCRP-CFCFRMS" (`SOP-DEF-FIRST-LAYER`, SOP 12(x)). In Wapsi, `layer == 1`. |
| **GSTIN** | Goods and Services Tax Identification Number. Wapsi uses an active GSTIN as one sign that a business account is a real merchant. |
| **Hold / lien** | A restriction on a specific amount in an account, while the rest of the balance stays usable. The SOP says "put on hold" (SOP 12(i)). Courts and banks often say "lien". Wapsi's holds are amount-specific by default; account-level action is only a recorded, supervisor-approved exception. |
| **IMPS** | Immediate Payment Service: NPCI's instant, 24x7 interbank transfer. `Channel.IMPS`. |
| **Innocent holder** | An account holder whose account received victim money without their knowledge, connivance or consent, for example a landlord paid rent by a mule (SOP 5(xii)). Split into a bona fide recipient and an unwitting conduit; disposition `HOLD_AMOUNT_NO_ESCALATION`; no escalation and no automatic STR (the officer decides). |
| **KYC** | Know Your Customer: identity verification of an account holder. Where an amount is put on hold, SOP 6.1(k) asks the bank to update the holder's KYC details on CFCFRMS within 1 week of reporting. Wapsi does not model this. |
| **Lakh** | 1,00,000 (one hundred thousand). Indian grouping writes ₹4,80,000.00, which is ₹4.8 lakh. |
| **Merchant** | A business paid for goods or services. SOP 9.2 separates delivered orders (no hold, update only) from undelivered orders (cancel and hold in the pool). `Role.MERCHANT`. |
| **Mule** | An account used to receive and move crime proceeds, often rented out or opened for the purpose. `Role.MULE`. |
| **NEFT** | National Electronic Funds Transfer: RBI's interbank transfer system, settled in batches. `Channel.NEFT`. |
| **Nodal / pool / escrow account** | Accounts that a payment aggregator, e-commerce company or other intermediary uses to collect and settle many customers' funds. SOP 9.1(iii): hold only the disputed amount, and normally no suspension or seizure. In Wapsi, `AccountType.NODAL`, `POOL` and `ESCROW`, disposition `HOLD_AMOUNT_INTERMEDIARY`. |
| **Nodal officer (bank)** | The bank officer who acts on CFCFRMS notices: places holds, updates the trail, answers LEAs (SOP 9.1). Wapsi's main user. |
| **PA / PG** | Payment aggregator / payment gateway: intermediaries that collect payments for merchants and settle them. SOP 9.5 (`SOP-9-5`). |
| **PoS** | Point of sale: a card terminal at a merchant. SOP 9.1(iv) escalates PoS exits to the acquirer or beneficiary bank where the amount was settled. `Channel.POS`. |
| **PPI** | Prepaid payment instrument, such as a wallet. SOP 9.3 (`SOP-9-3`). Not modelled in Wapsi. |
| **RRN** | Retrieval Reference Number: the 12-digit reference of a UPI or IMPS transaction. Wapsi's synthetic UPI and IMPS UTRs are 12 digits. |
| **RTGS** | Real Time Gross Settlement: RBI's system for large-value transfers, each settled individually. `Channel.RTGS`. |
| **TPAP** | Third-party application provider: a UPI app that is not a bank (SOP 9.5). |
| **Unwitting conduit** | An innocent holder that passed on at least half of the victim money it received: its account was used as a pass-through by someone else. `Role.UNWITTING_CONDUIT`; reason codes `UNWITTING_CONDUIT` and `STR_RECOMMENDED_OFFICER_DECISION`; an STR is recommended and the officer decides. |
| **UPI** | Unified Payments Interface: NPCI's real-time mobile payment system. `Channel.UPI`. |
| **UTR** | Unique Transaction Reference: the identifier of a transfer, quoted in hold notices as the UTR/TRX ID. NEFT and RTGS UTRs are alphanumeric; in the synthetic world they are the sending bank's id, `N` or `R`, and 11 digits. Wapsi also uses `ATM…` and `CSH…` references for synthetic cash withdrawals and deposits (see the [data contract](data-contract.md)). |
| **VASP** | Virtual asset service provider, such as a crypto exchange. A reporting entity under PMLA (SOP 12(xix)). Wapsi treats transfers to a VASP as an exit (`Channel.CRYPTO`). |
| **Whole-account freeze** | Blocking an entire account and its balance. The SOP discourages unwarranted freezing (`SOP-5-IX`). Wapsi's `baseline_freeze_amount` shows what one would lock up, for comparison only (the whole-account freeze baseline). |

## Wapsi terms

| Term | Meaning |
|---|---|
| **Account-level exception** | The only route to account-level action: an officer's request with at least one reason from the closed `AccountLevelReason` list, approved by a different supervisor. Disposition `EXCEPTIONAL_ACCOUNT_LEVEL_HOLD`; the amount-specific hold stays the recoverable figure ([ADR 0009](adr/0009-account-level-hold-as-recorded-exception.md)). |
| **Anchor** | A checkpoint of a case's audit log (event count and head hash) kept outside the log, so truncation and wholesale re-hashing are detectable (`wapsi.audit.ChainAnchor`). |
| **Audit event** | A record of each step (case opened, draft verified, approved, action taken) with actor, role and time (`AuditEvent`), sealed into the case's hash chain. |
| **Baseline freeze** | The whole-account freeze baseline: the total balance of every home-bank account that received victim money. It is what freezing them all would lock up (`HoldPlan.baseline_freeze_amount`), shown next to Wapsi's hold for comparison, never as a recommendation. |
| **Case** | One or more complaints traced together. Complaints join one case when their money credits the same account, or when one victim's account received another complaint's money (`group_complaints()`). |
| **Cash-out / exit** | Money leaving the banking system through an ATM or to a crypto exchange (`TerminalKind.ATM_CASH_OUT`, `CRYPTO_EXIT`). Nothing is left to hold. Disposition `CASHED_OUT`. |
| **Clock** | A deadline Wapsi tracks for a case, such as `STR_DUE`, `HOLD_LAPSE` or `FORWARD_HOLD_REVIEW`, with status `OPEN`, `WARNING`, `OVERDUE` or `DONE` (`src/wapsi/domain/clocks.py`). `Clock.notes` says how it was counted. |
| **Consortium follow** | Following money into other banks' accounts through simulated CFCFRMS responses (`TraceConfig.follow_consortium`). |
| **Disposition** | Wapsi's decision for one account: `HOLD_AMOUNT`, `HOLD_AMOUNT_NO_ESCALATION`, `HOLD_AMOUNT_INTERMEDIARY`, `HOLD_IN_POOL`, `FORWARD_HOLD`, `NO_HOLD_UPDATE_ONLY`, `OUTBOUND_CFCFRMS_REQUEST`, `CASHED_OUT` or `NO_ACTION`, plus `EXCEPTIONAL_ACCOUNT_LEVEL_HOLD` after an approved exception. `ESCALATE_S106_CANDIDATE` is reserved and never emitted. |
| **Draft** | A document Wapsi prepares for approval. There are eight kinds: hold instruction, customer notice, EDD memo, outbound request, STR, CFCFRMS update, restoration pack and the officer's account-level hold request (`DraftKind`). The LEA clarification letter is also template-bound. |
| **Fingerprint** | SHA-256 of a draft's kind, account, title, body, slots, structured data, citations and version. An approval applies to that exact fingerprint (`draft_fingerprint()`). |
| **Forward hold** | A lien on *future* credits to a first-layer account whose balance is below the victim money it received (SOP 9.1(i)). It is not money in hand, so it is never counted as recoverable (`forward_hold_amount`). |
| **Golden set** | Synthetic cases with known right answers, produced by an independent reference implementation (`src/wapsi/synth/reference.py`), used by `evals/harness.py`. |
| **Hard world** | A demo-sized synthetic world with five realistic confounders (sleeper mules, task-scam victim chains, a salary pool, a festival seller, same-second relays) and hard-mode noise on every typology (`uv run python -m wapsi.synth --size hard`). |
| **Hash chain** | The case's audit log as linked SHA-256 hashes: each event's hash covers its position, time, actor, role, type, case, a digest of its content and the previous hash (`wapsi.audit`). Editing, deleting or reordering an event breaks the chain. |
| **Holdable** | Victim money still sitting in home-bank accounts, other than nodal / pool / escrow accounts (counted as `intermediary`) and merchant money that paid for delivered goods (counted as `merchant_settled`) (`TraceTotals.holdable`). In Showcase 1 it is ₹1,23,648.00. |
| **Home bank** | The bank running Wapsi. In the synthetic data it is `HB01`. |
| **Identifiable amount** | For a police / LEA hold request: `min(plan hold for the account, victim money carried by the cited credits, amount requested)`, the most Wapsi will hold on it (`wapsi.verify.lea_request`). |
| **Layer** | How far an account is from the victim along the money trail. The victim account is layer 0, a first-layer account is layer 1, and so on (`AccountState.layer`). |
| **LEA request checklist** | The eight fields Wapsi checks in a police hold request: NCRP or FIR reference, officer and rank, jurisdiction, account number, specific amount, UTR / transaction references, BNSS provision, date and validity (`LeaField`). Missing fields go into a clarification letter. |
| **Maker-checker** | The rule that one person prepares and another approves. In Wapsi an analyst reviews the drafts and only a user with role `supervisor` can approve (`approve()`). |
| **Outbound request** | A request, sent through CFCFRMS, asking another bank to hold the victim money in an account it maintains. Disposition `OUTBOUND_CFCFRMS_REQUEST`, with hold 0 at the home bank. |
| **Over-hold** | Any amount held beyond the victim money in the account. It must be ₹0.00 (`HoldPlan.over_hold_amount`; verifier check `over_hold`). |
| **Pro-rata / FIFO / LIFO** | Ways to decide whose money leaves a mixed account. Pro-rata: in proportion to each victim's share (SOP 5(xiii), the default). FIFO: first in, first out. LIFO: last in, first out (`ApportionmentRule`). |
| **Reason code** | A machine-readable tag on a decision, such as `FORWARD_HOLD_FIRST_LAYER`, `ESCALATE_S106_CANDIDATE`, `HOLD_CAPPED_AT_BALANCE`, `NO_SUSPENSION_OR_SEIZURE`, `ROLE_UNKNOWN`, `VICTIM_ACCOUNT_ON_TRAIL`, `UNWITTING_CONDUIT`, `STR_RECOMMENDED_OFFICER_DECISION`, `OFFICER_REQUESTED_EXCEPTION` or `SUPERVISOR_APPROVED_EXCEPTION`. |
| **Recoverable** | Money that can go back to victims: home-bank holds plus outbound request amounts (`total_recoverable`, `per_victim_recoverable`). Forward holds are excluded. |
| **Regulatory radar** | A curated, read-only diff between the SOP Wapsi follows and new or pending rules, with verbatim quotes and per-case findings for a human to review (`data/docs/reg_radar.json`, `wapsi.domain.regradar`, the Regulatory Radar page). It never edits a plan, clock, draft or citation. |
| **requires_human** | A flag on a decision that a person must review it before anything is done: escalation flags, unknown roles, victim accounts on the trail, unwitting conduits and approved account-level exceptions. |
| **Showcase** | Two hand-computed synthetic cases in `src/wapsi/fixtures/showcase.py`. Every module must reproduce their numbers. |
| **Simulated CFCFRMS interface** | What Wapsi is towards CFCFRMS: it prepares updates and requests and records them, but sends nothing to the portal; the nodal officer submits there. Every view and draft that shows a CFCFRMS action says so. |
| **Skeleton** | A draft's title and body with a `⟦slot⟧` marker wherever a value was inserted. The verifier fills it with its own recomputed values and requires an exact match (see the [draft contract](draft-contract.md)). |
| **Slot** | A value in a draft (amount, account, UTR, date) that is filled from case data, never written by the LLM. |
| **Synthetic world** | A generated Indian banking dataset with banks, customers, mules, merchants, complaints and ground-truth labels (`uv run python -m wapsi.synth`). No real data is used. |
| **Taint / tainted amount** | Victim money, tracked per complaint (`ncrp_id`). `tainted_received_total` is how much an account received. `tainted_now` is how much is still in it. |
| **Terminal** | A point where the trace stops following the money: a merchant, a nodal / pool / escrow account, an other-bank account that is not followed, or an ATM or crypto exit (`TerminalKind`). |
| **Typology** | A fraud pattern in the synthetic world: `T1_FANOUT_CASHOUT` to `T10_NODAL_POOL` in every world, plus `T11_SLEEPER_MULE` to `T15_INSTANT_RELAY` in the hard world. |
| **Unlocated** | Victim money the trace could not place. In the showcase it is ₹0.00 (`TraceTotals.unlocated`). |
| **Verifier** | The checks every draft must pass before approval: numbers, citations, policy rules and untrusted-text screening (`src/wapsi/verify/`). |

## Platforms and events

Wapsi ships one portable core and three platform editions. See [Editions](editions.md).

| Term | Meaning |
|---|---|
| **ADK** | Google's Agent Development Kit. The Google Cloud edition runs ADK agents on Cloud Run (`editions/gcp/README.md`). |
| **AMD Instinct MI300X / ROCm** | AMD's data-centre GPU and its open GPU software stack. The AMD edition serves open models with vLLM on ROCm on a single MI300X (`editions/amd/README.md`). |
| **CoCo** | Snowflake Cortex Code, Snowflake's AI coding agent. CoCo sessions deploy the Snowflake edition, build its semantic view and agent, and prove it live (`editions/snowflake/COCO_SPRINT.md`). |
| **GCC** | Global Capability Centre: an in-house centre, often in India, that runs technology or operations for a parent company, such as a bank's fraud operations. The Snowflake edition is entered in the GCC edition of the Snowflake CoCo CLI hackathon. It does not mean the Gulf Cooperation Council here. |
| **Streamlit** | The Python web framework used for Wapsi's officer UI (`ui/app.py`). The Snowflake edition runs it in Snowflake. |
