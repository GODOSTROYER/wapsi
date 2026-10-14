# A/B: raw LLM vs Wapsi-bound LLM (demo, 20 cases)

- Label: **AMD ROCm notebook run, 2026-10-14**
- LLM: `vllm-rocm:wapsi-llm` · generated 2026-10-14T14:54:42Z
- Compared: 20 of 20 cases (both arms completed them); case errors: raw 0, bound 0

Like with like, one STR per case in each arm:

| | Raw LLM, free-form STR | Wapsi-bound STR (slots + verifier) |
|---|---:|---:|
| STRs | 20 | 17 |
| STRs with a wrong amount | 4 | 0 |
| Invented account numbers | 0 | 0 |
| Unknown or repealed citations | 0 | 0 |
| STRs passing the verifier (raw: the same audit) | 0.8 | 1.0 |

Every draft of the bound arm (hold instructions, CFCFRMS updates, STR, restoration, notices):

| | Raw LLM, free-form STR | Wapsi-bound (slots + verifier) |
|---|---:|---:|
| Documents | 20 | 155 |
| Documents with a wrong amount | 4 | 0 |
| Wrong amount mentions | 4 | 0 |
| Invented account numbers | 0 | 0 |
| Unknown or repealed citations | 0 | 0 |
| Clean documents (pass the audit; bound: and the verifier) | 16 | 155 |
| Clean rate | 0.8 | 1.0 |

Bound arm: 7 of 43 LLM prose fields were rejected by the checks and replaced by fixed text (rate 0.1628); verifier pass rate 1.0.
