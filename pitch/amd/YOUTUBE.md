# YouTube upload kit

Two videos. Upload both as **Public** or **Unlisted** (never Private: judges cannot open it). Category: Science &
Technology. Not made for kids. Every figure below is a row of [`../NUMBERS.md`](../NUMBERS.md).

## 1. Pitch video (submit this one in the lablab "demo video" field)

- **File:** `pitch/amd/video/wapsi_amd_demo_draft.mp4` (4:08, 1920x1080; built locally, not in git)
- **Thumbnail:** [`cover/wapsi_youtube_thumbnail_1280x720.png`](cover/wapsi_youtube_thumbnail_1280x720.png)
- **Captions:** [`video/captions.srt`](video/captions.srt) (also burned in)

**Title**

```
Wapsi: Hold the Fraud, Not the Shop | Gemma 4 on AMD Instinct MI300X | AMD Developer Hackathon
```

**Description**

```
When stolen money lands in an honest shop's account, Indian banks often freeze the whole account. The victim still waits, and the shop stops trading. India held ₹7,647 Cr for cyber-fraud victims and restored ₹167 Cr: 2.18% of the money held (MHA/I4C SOP, 2026).

Wapsi is a copilot for a bank's cyber-fraud nodal officer. It traces the money trail through mule layers, computes the exact tainted amount per account, and recommends holding only that amount, not the whole account. It drafts the STR and restoration paperwork, verifies every number and citation, and requires maker-checker approval with a tamper-evident audit chain.

Numbers come from data, words come from the model: Gemma 4 writes the prose, and a verifier recomputes every rupee.

Measured on AMD Instinct MI300X (AMD AI Notebook, ROCm 7.14, vLLM, 14 Oct 2026), Gemma 4 31B self-served:
• 1,590 complete cases per hour, $1.25 per 1,000 cases
• 773 output tokens/s on real drafting prompts
• Every draft passed the verifier; holds exact to the paisa
• Raw Gemma 4 got an amount wrong in 19 of 39 cases; inside Wapsi, 0 of 155 drafts

Built with Google Gemma 4 and the Google Agent Development Kit (ADK). AMD Developer Hackathon ACT III, Track 3: Reinvent Commerce.

All data is synthetic. Simulated CFCFRMS interface.

Chapters
0:00 The shop that was frozen
0:28 What Wapsi does
0:44 One case, end to end
1:39 Voice intake and the agent trace
2:22 Measured on AMD Instinct MI300X
3:41 Google Gemma 4 and the ADK
4:00 Close

Live app: https://wapsi-arnavbule.streamlit.app/
Showcase: https://godostroyer-wapsi.static.hf.space/
Code (MIT): https://github.com/GODOSTROYER/wapsi

#AMD #MI300X #ROCm #Gemma4 #GoogleADK #FraudDetection #FinTech #lablab
```

**Tags:** AMD, MI300X, ROCm, Gemma 4, Google ADK, vLLM, fraud detection, money mule, banking, fintech, India, UPI
fraud, AI agent, hackathon, lablab

## 2. Use-case walkthrough

See the section added below when the walkthrough video is built.
