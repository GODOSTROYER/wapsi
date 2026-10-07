# CoCo GCC submission brief (limit 1,024 characters)

Problem statement 1: Risk, Fraud & Regulatory copilot. Paste the text between the markers into the form as it is.

<!-- limit:1024 -->
Of ₹7,647 Cr of cyber-fraud money held via CFCFRMS (Apr 2021 to Nov 2025), victims got back ₹167 Cr: 2.18% (MHA SOP 2026). Banks freeze whole accounts; innocent holders, merchants and pool accounts get locked and restoration stalls.

Wapsi is a Snowflake-native copilot for the bank's nodal officer. Snowpark procedures trace a 1930 complaint's money trail, apportion taint pro-rata (SOP Annexure-V, reproduced to the paisa) and hold only victim money. Cortex AI_COMPLETE writes prose only: every amount, account, UTR and date is a slot from tables, recomputed by a verifier before a supervisor approves. Cortex Search grounds citations; a semantic view and Cortex Agent answer officer questions; Streamlit in Snowflake is the case room. 6 CoCo skills drive it; a hook blocks DROP and hand-edited holds.

On a 39-case synthetic world, a whole-account freeze locks ₹35.26 Cr in 101 accounts; Wapsi holds ₹29.19 L in 84: 0 false freezes, holds exact to the paisa.
<!-- /limit -->

Length: 961 / 1,024 characters (969 bytes in UTF-8, because ₹ is 3 bytes, so it fits under either way of counting).
Recount after any edit with `uv run python pitch/check_numbers.py`.

## Before you paste (Oct 3)

- Every number above is in [`../NUMBERS.md`](../NUMBERS.md): `sop_held`, `sop_restored`, `sop_restored_pct`,
  `demo_freeze_cr`, `demo_freeze_accounts`, `demo_hold_l`, `demo_hold_accounts`, `demo_false_freeze`,
  `demo_hold_exact`, `skills_n`.
- If the C3 session produced a real-LLM result on Cortex AI_COMPLETE, swap the last sentence's tail for it (keep under
  the limit), and add the row to `NUMBERS.md` first.
- If the form counts line breaks differently, replace the two blank lines with single spaces: that only shortens it.
