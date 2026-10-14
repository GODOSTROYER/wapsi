# lablab.ai submission fields (AMD Developer Hackathon ACT III)

Submit by **18 Oct 2026, 17:30 IST** (our internal stop). The official deadline is 20:30 IST, 15:00 UTC. Counts
are checked by `uv run python pitch/check_numbers.py`; every number is a row in [`../NUMBERS.md`](../NUMBERS.md).
After editing a limited block, run `uv run python pitch/check_numbers.py --fix-lengths`.

The form asks for: title, short description, long description, selected challenge, technology tags, a working
prototype, cover image, demo video, pitch presentation, public GitHub repository, demo application platform and
application URL.

## Project title (limit 50 characters)

<!-- limit:50 -->
Wapsi: Hold the Fraud, Not the Shop
<!-- /limit -->

Length: 35 / 50 characters

## Short description (limit 255 characters)

<!-- limit:255 -->
When fraud money lands in an honest shop's account, banks freeze the whole account. Wapsi traces the money, holds only the victim's share, keeps merchants trading and drafts verified paperwork, with Gemma 4 on AMD GPUs and a transparent mule scorer.
<!-- /limit -->

Length: 249 / 255 characters

## Long description (target 1,500 characters)

<!-- limit:1500 -->
A kirana shop sells groceries to a buyer spending stolen money. Next morning its whole account is frozen. India held ₹7,647 Cr for cyber-fraud victims and restored ₹167 Cr: 2.18% (MHA/I4C SOP, 2026). Whole-account freezes lock honest merchants, customers and payment pools, and the money still does not go back.

Wapsi is a copilot for a bank's fraud desk. From the bank's own transaction data it traces a complaint's money through mule accounts, splits each balance between victims by the SOP's method (reproduced to the paisa), and proposes one action per account: hold only the victim's money, no hold for a merchant that delivered, a request to the next bank, a restoration packet.

Which account is a mule, an innocent holder or a merchant is predicted by a transparent scorer (a GNN trained on an AMD MI300X adds a second opinion), never by a language model. Amounts are arithmetic.

Gemma 4 on AMD GPUs (AMD's Token Factory, or vLLM on an MI300X) writes the sentences of each draft and reads Hinglish complaints and voice notes. Every number is filled from data and recomputed by a verifier before a supervisor approves. On one MI300X: 1,590 cases an hour, every draft verified. A Google ADK agent on it logs every tool call.

On a 39-case synthetic test world a whole-account freeze locks ₹35.26 Cr in 101 accounts; Wapsi holds ₹29.19 L in 84, with 0 false freezes and every hold exact. For honest merchants and customers: ₹82.99 L frozen against ₹2.50 L held. All data is synthetic.
<!-- /limit -->

Length: 1,491 / 1,500 characters

## Selected challenge

**Track 3: Reinvent Commerce** ("Help a business understand its customers and take better action").
How the entry meets each point of the track brief: [`../TRACK_FIT.md`](../TRACK_FIT.md).

Partner prizes: the event page says a project can qualify for the main challenge prize, one partner prize, or
both. Our partner prize is **Google** (Gemma 4 and the Agent Development Kit; see
[`../GOOGLE_PRIZE.md`](../GOOGLE_PRIZE.md)). The Evolus track is prepared as an alternative
([`../EVOLUS_TRACK.md`](../EVOLUS_TRACK.md)). **TODO (user):** confirm on the submission form whether one or two
partner prizes can be selected, and pick accordingly.

## Technologies and tags

AMD Token Factory · AMD Instinct · ROCm · vLLM · Gemma 4 · Google ADK (Agent Development Kit) · LiteLLM ·
Whisper · PyTorch · Graph Neural Network · Streamlit · FastAPI · Docker · Python · Fintech · Fraud · Commerce

These are the tags of the stack the submitted build uses. **AMD Instinct**, **ROCm**, **vLLM** and **Graph Neural
Network** are in because the AMD AI Notebook run on one MI300X is committed
(`editions/amd/evidence/notebook/2026-10-14/`). Add **AMD Developer Cloud** only after a droplet run is committed;
the notebook is not the droplet.

## Cover image brief

- **Format:** 16:9, 1920 × 1080, readable as a thumbnail.
- **Left half:** a small shop front (kirana) with a padlock over it, greyed out. Caption: "Whole-account freeze".
- **Right half:** the same shop open and lit, with one small coin set aside in a marked box. Caption: "Wapsi:
  hold the fraud, not the shop".
- **Bottom strip, the only numbers:** `₹35.26 Cr frozen` → `₹29.19 L held` · `0 false freezes`, and in small
  type "39-case synthetic test world". Optional second line, the retention figure: `honest merchants and customers:
  ₹82.99 L frozen → ₹2.50 L held`.
- **Corner marks:** "Gemma 4 on AMD GPUs" and "Track 3: Reinvent Commerce". Use AMD and Google names as plain
  text; do not use their logos unless the event's brand kit allows it.
- **Do not show:** real bank names, real people, account numbers, or any figure that is not a row in
  `NUMBERS.md`.

## Links (fill on submission day)

| Field | Value |
|---|---|
| Demo video (limit 5:00) | **TODO (user):** link of the 4:15 cut, [`VIDEO_SCRIPT.md`](VIDEO_SCRIPT.md) |
| Pitch presentation | **TODO (user):** PDF exported from [`DECK_OUTLINE.md`](DECK_OUTLINE.md) |
| Public GitHub repository (MIT) | **TODO (user):** repository URL; it must contain the tag `amd-baseline`. Push explicit refs only: `git push origin main amd-baseline`. Never `git push --tags` or `--all`: local refs from earlier work (another event's tag and branch) must stay off the public repository |
| Demo application platform | **TODO (user):** where the app is hosted (container host or the AMD instance) |
| Application URL | **TODO (user):** the public URL; the page states that all data is synthetic |
| Cover image | **TODO (user):** image made from the brief above |

## Originality statement (paste into the long description's last line or the README section it links to)

Wapsi's portable core existed before the event and is tagged `amd-baseline` (last pre-event commit dated
2026-10-07). Built during the event, 12 to 18 Oct 2026: Gemma 4 on AMD as the drafting and extraction model, the
Google ADK agent on Gemma 4, open-model voice intake, the Track 3 product story and every measurement that names
AMD. Details and commit ranges: `docs/originality.md`.
