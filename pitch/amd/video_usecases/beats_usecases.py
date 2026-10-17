"""The use-case video, beat by beat: who uses Wapsi, what they do in it, and what they get. Plain words for judges
and non-experts. Same format and pipeline as the pitch video (pitch/amd/video/scripts/beats.py):

    uv run python pitch/amd/video/scripts/build_video.py --engine gemini --voice Charon \
        --beats pitch/amd/video_usecases/beats_usecases.py --out-dir pitch/amd/video_usecases

`say` is read by the voice (numbers spelled out), `cap` is the burned-in caption (only figures registered in
pitch/NUMBERS.md "Pitched as"), `vis` is a slide from slides/ (slides_usecases.html) or an app screenshot pan from
shots/ (capture_usecases.py), falling back to the pitch video's slides/ and shots/.
"""

from __future__ import annotations

OUT_NAME = "wapsi_usecases.mp4"
MAX_SECONDS = 240
SCRIPT_TITLE = "# Wapsi · use-case walkthrough video: final narration"
SCRIPT_NOTES = (
    "Who uses Wapsi, what they do in it, and what they get, in plain words. App screens: the local Streamlit app "
    "in recorded mode (prose recorded from Gemma-4-31B-IT on AMD Token Factory, 14-10-2026), showcase case "
    "CASE-31009260000101; the agent trace was recorded on AMD Token Factory. Headline figures are the pitch's "
    "registered rows (pitch/NUMBERS.md) and each says where it ran on screen: the 39-case golden set and the "
    "throughput / cost figures were measured 14 Oct 2026 on an AMD Instinct MI300X (AMD AI Notebook). "
    "Synthetic data only."
)

SOURCES = {
    "showcase": "src/wapsi/fixtures/showcase.py via pitch/NUMBERS.md rows sc1_* (showcase world)",
    "lea": "the app's police / LEA check on the showcase case (wapsi.verify.lea_request; hold limited to sc1_hold_a)",
    "facts": "data/docs/facts.json via pitch/NUMBERS.md rows str_days, hold_lapse_days",
    "drill": "uv run python -m wapsi.audit drill (pitch/NUMBERS.md row cmd_drill; laptop, synthetic showcase case)",
    "golden": "editions/amd/evidence/notebook/2026-10-14/golden_eval.json (MI300X, session 4) = NUMBERS.md rows "
              "demo_freeze, demo_hold, nb_golden_false_freezes, nb_raw_wrong",
    "mi300x": "editions/amd/evidence/notebook/2026-10-14/evidence.json, bench/e2e_c16.json = NUMBERS.md rows "
              "amd_e2e_best_cases_per_hour, nb_usd_per_1000, amd_ab_bound_docs",
}

MAIN = (300, 1620)  # x, w: the app's main column, without the sidebar

SCENES: list[dict] = [
    {
        "title": "The frozen shop",
        "beats": [
            dict(say="A kirana shop sells twelve hundred rupees of groceries to a stranger.",
                 cap="A kirana shop sells ₹1,200 of groceries to a stranger.", vis=("slide", "hook_1"), src="showcase"),
            dict(say="The stranger was spending stolen money. So the bank freezes the shop's whole account.",
                 cap="The stranger was spending stolen money. So the bank freezes the shop's whole account.",
                 vis=("slide", "hook_2"), src="showcase"),
            dict(say="Wapsi holds only the stolen amount. Here is how seven different people use it.",
                 cap="Wapsi holds only the stolen amount. Here is how seven different people use it.",
                 vis=("slide", "cast"), src=None),
        ],
    },
    {
        "title": "1 · The bank's nodal officer",
        "beats": [
            dict(say="First, the bank's nodal officer. Fraud complaints from the nineteen-thirty helpline land in her inbox.",
                 cap="First, the bank's nodal officer. Fraud complaints from the 1930 helpline land in her inbox.",
                 vis=("slide", "p_officer"), src=None),
            dict(say="She opens one. Four lakh, eighty thousand rupees were stolen from a customer.",
                 cap="She opens one: ₹4,80,000 stolen from a customer.",
                 vis=("ui", "inbox", *MAIN, 0, 120), src="showcase"),
            dict(say="Wapsi shows where the money went, hop by hop, through four layers of accounts.",
                 cap="Wapsi shows where the money went, hop by hop, through four layers of accounts.",
                 vis=("ui", "case_trail", *MAIN, 0, 340), src="showcase"),
            dict(say="Then a hold plan: for each account, the exact amount to hold, and the rule in the government's procedure behind it.",
                 cap="A hold plan: for each account, the exact amount to hold, and the SOP rule behind it.",
                 vis=("ui", "hold_plan", *MAIN, 560, 900), src=None),
            dict(say="In total, one lakh, forty-two thousand, eight hundred and forty-eight rupees held, instead of more than five crore frozen.",
                 cap="₹1,42,848 held, instead of ₹5,03,50,000 frozen.",
                 vis=("ui", "hold_plan", *MAIN, 200, 240), src="showcase"),
        ],
    },
    {
        "title": "2 · The supervisor",
        "beats": [
            dict(say="Next, the supervisor. Nothing leaves the bank without a second person.",
                 cap="Next, the supervisor. Nothing leaves the bank without a second person.",
                 vis=("slide", "p_supervisor"), src=None),
            dict(say="The analyst who prepared the plan cannot approve it. Every item waits for a supervisor.",
                 cap="The analyst who prepared the plan cannot approve it. Every item waits for a supervisor.",
                 vis=("ui", "approvals_analyst", *MAIN, 0, 300), src=None),
            dict(say="The supervisor approves. The approval is stamped with a fingerprint of exactly what was approved.",
                 cap="The supervisor approves. The approval is stamped with a fingerprint of exactly what was approved.",
                 vis=("ui", "audit_fingerprint", *MAIN, 0, 120), src=None),
            dict(say="Freezing a whole account is only an exception: a reason from a fixed list, recorded, and approved by someone else.",
                 cap="Freezing a whole account is only an exception: a reason from a fixed list, recorded, approved by someone else.",
                 vis=("ui", "exception", *MAIN, 1450, 1560), src=None),
        ],
    },
    {
        "title": "3 · Call-centre and branch staff",
        "beats": [
            dict(say="Call-centre and branch staff take complaints by phone, often in Hinglish. They upload the voice note.",
                 cap="Call-centre and branch staff take complaints by phone, often in Hinglish. They upload the voice note.",
                 vis=("slide", "p_staff"), src=None),
            dict(say="Wapsi writes out the fields: who, how much, when, which account. Then it checks every claim against the bank's own ledger.",
                 cap="Wapsi writes out the fields, then checks every claim against the bank's own ledger.",
                 vis=("ui", "voice_result", *MAIN, 1100, 1360), src=None),
        ],
    },
    {
        "title": "4 · The police",
        "beats": [
            dict(say="The police send hold requests. Many are incomplete, or ask to freeze everything.",
                 cap="The police send hold requests. Many are incomplete, or ask to freeze everything.",
                 vis=("slide", "p_police"), src=None),
            dict(say="Wapsi checks each request against an eight-point checklist. This one has four of the eight.",
                 cap="Wapsi checks each request against an eight-point checklist. This one has four of the eight.",
                 vis=("ui", "lea_check", *MAIN, 2000, 2100), src="lea"),
            dict(say="The hold is limited to what can be traced: forty-three thousand, two hundred rupees, not the whole account. A letter asks for the missing details.",
                 cap="The hold is limited to what can be traced: ₹43,200, not the whole account. A letter asks for the missing details.",
                 vis=("ui", "lea_check", *MAIN, 2450, 2560), src="lea"),
        ],
    },
    {
        "title": "5 · Compliance and audit",
        "beats": [
            dict(say="Compliance teams and auditors need proof, months later.",
                 cap="Compliance teams and auditors need proof, months later.",
                 vis=("slide", "p_audit"), src=None),
            dict(say="Every action goes into a hash-chained log. Each entry locks the one before it.",
                 cap="Every action goes into a hash-chained log. Each entry locks the one before it.",
                 vis=("ui", "audit", *MAIN, 0, 220), src=None),
            dict(say="We tried six ways to tamper with it: editing, deleting, reordering. All six were caught.",
                 cap="We tried six ways to tamper with it: editing, deleting, reordering. All six were caught.",
                 vis=("slide", "drill"), src="drill"),
            dict(say="And deadlines are counted for them: the S T R is due within seven working days, and a hold may lapse after ninety days.",
                 cap="Deadlines are counted: the STR is due within 7 working days; a hold may lapse after 90 days.",
                 vis=("ui", "clocks", *MAIN, 0, 260), src="facts"),
        ],
    },
    {
        "title": "6 · The shop and the victim",
        "beats": [
            dict(say="Now the people this is really for. The kirana shop delivered its goods: no hold. It keeps trading.",
                 cap="The people this is for. The kirana shop delivered its goods: no hold. It keeps trading.",
                 vis=("slide", "honest"), src="showcase"),
            dict(say="Of everything a full freeze would lock, ninety-nine point seven five percent belongs to people who did nothing wrong.",
                 cap="99.75% of what the freeze locks belongs to people who did nothing wrong.",
                 vis=("slide", "honest"), src="showcase"),
            dict(say="And the victim: three lakh, thirty-four thousand, eight hundred and forty-eight rupees is traced and recoverable.",
                 cap="And the victim: ₹3,34,848 traced and recoverable.",
                 vis=("slide", "honest"), src="showcase"),
        ],
    },
    {
        "title": "7 · Ask the agent",
        "beats": [
            dict(say="Anyone on the team can simply ask. Open this case. An agent built on Google's Agent Development Kit answers.",
                 cap="Anyone on the team can simply ask. An agent built on Google's Agent Development Kit answers.",
                 vis=("ui", "agent_trace", *MAIN, 280, 600), src=None),
            dict(say="It calls Wapsi's tools. Every number in its answer comes from a tool, never from the model's memory.",
                 cap="It calls Wapsi's tools. Every number in its answer comes from a tool, never from the model's memory.",
                 vis=("ui", "agent_trace", *MAIN, 600, 900), src=None),
            dict(say="And it follows the rules. Asked by an analyst to approve, it refuses.",
                 cap="And it follows the rules. Asked by an analyst to approve, it refuses.",
                 vis=("ui", "agent_trace", *MAIN, 2260, 2424), src=None),
        ],
    },
    {
        "title": "Why this wins",
        "beats": [
            dict(say="Why this wins. Numbers come from the data; the model only writes the words, and a verifier recomputes every rupee. Every rule cites the government's procedure. A second person approves, and the log shows any tampering. And it runs on open Gemma 4, on the bank's own AMD GPU.",
                 cap="Numbers from data, words from the model, every rupee recomputed. Rules cite the SOP. Maker-checker, tamper-evident log. Open Gemma 4 on the bank's own AMD GPU.",
                 vis=("slide", "moats"), src=None),
            dict(say="On a thirty-nine-case test world, run on an AMD Instinct MI300X, a full freeze locks thirty-five point two six crore. Wapsi holds twenty-nine point one nine lakh, with zero false freezes.",
                 cap="39-case test world, AMD Instinct MI300X: freeze ₹35.26 Cr; Wapsi holds ₹29.19 L, 0 false freezes.",
                 vis=("slide", "metrics_1"), src="golden"),
            dict(say="One MI300X handles one thousand, five hundred and ninety cases an hour, at about a dollar twenty-five per thousand cases.",
                 cap="One MI300X: 1,590 cases an hour, $1.25 per 1,000 cases.",
                 vis=("slide", "metrics_2"), src="mi300x"),
            dict(say="Raw Gemma 4 got an amount wrong in nineteen of thirty-nine cases. Bound by Wapsi: zero of one hundred and fifty-five drafts. Every draft verified.",
                 cap="Raw Gemma 4: a wrong amount in 19 of 39 cases. Wapsi-bound: 0 of 155 drafts. Every draft verified.",
                 vis=("slide", "metrics_3"), src="golden"),
        ],
    },
    {
        "title": "Close",
        "beats": [
            dict(say="Wapsi. Hold the fraud, not the shop. Try the live app, or read the code. All data is synthetic.",
                 cap="Wapsi. Hold the fraud, not the shop. Live app and code on screen. Synthetic data only.",
                 vis=("slide", "close_uc"), src=None),
        ],
    },
]
