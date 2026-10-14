# Pitch pack

For the lablab.ai **AMD Developer Hackathon ACT III**, Track 3 "Reinvent Commerce". Judges: start at
[`START_HERE_FOR_JUDGES.md`](START_HERE_FOR_JUDGES.md).

| Folder / file | For | What is in it |
|---|---|---|
| [`START_HERE_FOR_JUDGES.md`](START_HERE_FOR_JUDGES.md) | judges | the 60-second hook, headline numbers, a 2-minute tour, links to the code |
| [`TRACK_FIT.md`](TRACK_FIT.md) | judges | the Track 3 brief, line by line, against the evidence in the repository |
| [`amd/`](amd/) | the submission | deck outline, video script, lablab form fields and the cover-image brief |
| [`GOOGLE_PRIZE.md`](GOOGLE_PRIZE.md) | the Google partner prize | Gemma 4 and the Agent Development Kit on AMD: the plan and its evidence slots |
| [`EVOLUS_TRACK.md`](EVOLUS_TRACK.md) | the Evolus partner track | a plan: the process, the building blocks, the API calls, what must be created by hand |
| [`NUMBERS.md`](NUMBERS.md) | the team | every number used anywhere in `pitch/`, mapped to its source file and field; the open AMD measurement slots |
| [`check_numbers.py`](check_numbers.py) | the team | re-reads every source; fails on a stale number, an unregistered amount, a form over its limit or an unlisted open slot |
| [`amd/evidence/`](amd/evidence/) | the team | a live smoke run against the AMD endpoint (never part of the tests) and the results files it writes |

Before any number goes on a slide or into a form:

```bash
uv run python pitch/check_numbers.py                # numbers, registered amounts, character limits, open slots
uv run python pitch/check_numbers.py --list         # also print every resolved number and every open slot
uv run python pitch/check_numbers.py --fix-lengths  # after editing a limited block (lablab fields)
```

The same checks run in the test suite (`tests/pitch/`), so a regenerated report that changes a pitched number
fails the build until the pitch is updated.

## Filling an AMD measurement slot

A figure that has not been measured on AMD is written `TBD(slot_id)` and listed at the end of `NUMBERS.md`. To
fill one:

1. Check the run was healthy (`amd/evidence/token_factory_smoke.py --probe-only` before and after it, and a
   look at a few draft bodies): the verifier checks numbers, not prose quality, so a degraded endpoint can still
   produce a "pass". Then commit the results file (for example a harness report under `evals/reports/`, or a file
   written by `amd/evidence/token_factory_smoke.py`).
2. Add a row with a locator to a numbers table in `NUMBERS.md`. Its "Pitched as" text says where the figure ran:
   AMD Token Factory, an AMD GPU notebook or an MI300X droplet.
3. Replace `TBD(slot_id)` in the deck, the video script and the other pages with the figure.
4. Delete the slot's row, then run `check_numbers.py`.

Never fill a slot from memory, from a CPU run or from a rehearsal. A slot that is still open on recording day has
its sentence cut from the video.
