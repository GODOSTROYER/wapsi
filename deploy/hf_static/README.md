---
title: Wapsi
emoji: 🧾
colorFrom: red
colorTo: green
sdk: static
pinned: true
license: mit
short_description: Hold the tainted amount, not the whole account
---

# Wapsi: hold the tainted amount, not the whole account

Static showcase page for **Wapsi**, a cyber-fraud money-trail copilot for a bank's nodal officer
(lablab.ai AMD Developer Hackathon ACT III, Track 3 "Reinvent Commerce"; Gemma 4 + Google ADK).

- Code: https://github.com/GODOSTROYER/wapsi
- Every figure on the page says where it ran and traces to `pitch/NUMBERS.md` in the repository.
- All case data is synthetic. CFCFRMS is a simulated interface. MIT licence.

## Editing

The live-app and YouTube links are constants at the top of `index.html` (`LIVE_APP_URL`, `YOUTUBE_URL`).
Source lives in `deploy/hf_static/` of the GitHub repository; the demo MP4 (`assets/wapsi_demo.mp4`, 1280x720)
is uploaded to the Space only and kept out of git. Re-upload with `python deploy/hf_static/publish.py`
(needs `HF_TOKEN` in the environment).
