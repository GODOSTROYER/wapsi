# Whisper on an AMD Instinct MI300X: real-time factor, 14 October 2026

`whisper_rtf.json` is one pass of `openai/whisper-large-v3-turbo` over the six bundled synthetic voice notes
(`data/samples/voice/*.wav`, 16 kHz mono, 26 to 31 seconds each), measured on an AMD Instinct MI300X (AMD AI
Notebook, ROCm 7.14). It ran at 15:25 UTC in GPU session 5, the session (and MI300X) that produced the rest of
`editions/amd/evidence/notebook/2026-10-14/` except the golden set, which ran in session 4 on another MI300X
(`SESSIONS.md` there lists the sessions and both GPUs).

| | Value | Field |
|---|---|---|
| Device / torch | AMD Instinct MI300X VF / 2.11.0+rocm7.14.1 | `device`, `torch` |
| Model load | 4.64 s | `load_s` |
| Steady-state real-time factor (samples 2 to 6) | 0.0312 | `steady_state_rtf_excluding_first` |
| The 30.93-second note (Meera Joshi) | transcribed in 0.921 s | `samples[meera_impersonation.wav].asr_s` |
| First sample, kernel warm-up included | 9.394 s for 27.97 s of audio (RTF 0.3359) | `samples[0]` |
| Mean over all six, warm-up included | 0.082 | `mean_rtf` |

A real-time factor of about 0.03 means a 30-second note takes about one second to transcribe once the GPU is warm.

## How it ran

- **Run by hand.** The sprint's voice block (`editions/amd/kickoff/session_sprint.sh`) failed in session 5: the
  notebook image has no `ffmpeg`, and the transformers pipeline needs it to open an audio file by path. The same
  model was then run from the notebook's Python with each WAV decoded by the standard-library `wave` module and
  passed to the pipeline as `{"raw": <float32 samples>, "sampling_rate": 16000}`, fp16, `return_timestamps=True`.
  The sprint's voice block now does exactly that.
- **What it measures.** Speech-to-text time only. The transcripts were not saved and the notes were not reconciled
  with the ledger in this run, so how many notes reconcile with Whisper in the loop is still the open slot
  `voice_asr_samples` in `pitch/NUMBERS.md`.
- **Six synthetic samples, one pass.** A timing run, not an evaluation of transcription accuracy.

## Provenance

- Written on the notebook at `measured_at` (2026-10-14T15:25:09Z), inside the event window, and copied here
  unchanged.
- Re-run on an AMD GPU notebook: `SPRINT_BLOCKS="voice" bash editions/amd/kickoff/session_sprint.sh` (writes
  `editions/amd/bench/results/voice-notebook-<UTC date>/whisper_rtf.json`).
