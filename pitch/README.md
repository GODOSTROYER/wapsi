# Pitch pack

Judges: start at [`START_HERE_FOR_JUDGES.md`](START_HERE_FOR_JUDGES.md).

| Folder / file | For | What is in it |
|---|---|---|
| [`START_HERE_FOR_JUDGES.md`](START_HERE_FOR_JUDGES.md) | both events | the 60-second hook, headline numbers, a 2-minute tour, links to the code |
| [`NUMBERS.md`](NUMBERS.md) | the team | every number used anywhere in `pitch/`, mapped to its source file and field |
| [`check_numbers.py`](check_numbers.py) | the team | re-reads every source; fails on a stale number, an unregistered amount or a form over its limit |
| [`aibc/`](aibc/) | AI Builder Cup 2026 (Google Cloud) | video script, PDF deck outline, submission form drafts |
| [`amd/`](amd/) | AMD Developer Hackathon ACT III | deck outline, video script, lablab fields |

Before any number goes on a slide or into a form:

```bash
uv run python pitch/check_numbers.py                # numbers, registered amounts, character limits
uv run python pitch/check_numbers.py --fix-lengths  # after editing a limited block (lablab fields, form text)
```

The same checks run in the test suite (`tests/pitch/test_pitch_pack.py`), so a regenerated report that changes a
pitched number fails the build until the pitch is updated.
