# data/docs: legal and regulatory corpus

This folder holds the public texts behind every citation Wapsi makes, plus a local BM25 search over them
(`wapsi.search.index.LocalDocSearch`, which implements `wapsi.ports.DocSearch`).

## Build it

```
uv run python -m wapsi.search.fetch             # download to raw/ (sha256 + retrieval time -> MANIFEST.md)
uv run python -m wapsi.search.build             # raw/ -> index/chunks.jsonl (+ index/text/, build_report.json)
uv run python -m wapsi.search.check_citations   # check every quote and fact against raw/; exit 1 on mismatch
uv run python -m wapsi.search.sources           # regenerate sources.json from wapsi/search/sources.py
```

`fetch` identifies itself with a User-Agent, pauses 1 s between requests, retries 5xx errors and timeouts,
and falls back to mirror URLs. It never turns off TLS verification. It rejects wrong content, such as an
HTML bot-challenge page where a PDF or known text was expected. Files already in `raw/` are reused
(`--force` downloads them again; `--probe-mirrors` also tests each mirror).

`build` and `check_citations` need `pypdf` (the `docs` extra). `LocalDocSearch.load()` needs neither
pypdf nor the network. If `index/` is missing, it warns and searches a small fallback corpus made from the
quotes in `citations.json`.

Set `WAPSI_DOCS_DIR` to use a copy of this folder somewhere else (for example Snowflake or Cloud Run).

## Sources

| id | document | used for |
|---|---|---|
| `sop-2026` | MHA/I4C SOP for NCRP-CFCFRMS, Custody, Restoration of money and Grievance Redressal (02.01.2026), from the High Court of Uttarakhand circular of 18.06.2026 | `SOP-*`, and the SOP's own summaries of Dr Sajeer and Mohammed Saifullah (Annexure-III) and of the FIU-IND circular of 02.06.2025 (Para 9.1(ii)) |
| `bnss-2023` | Bharatiya Nagarik Suraksha Sanhita, 2023, consolidated India Code PDF | `BNSS-94/106/107/168` (the index keeps ss. 94, 106, 107, 168, 173, 497, 498, 503, 504) |
| `pmlr-2005` | PML (Maintenance of Records) Rules, 2005, consolidated text from FIU-IND | `PMLR-2-1-G` |
| `fiu-faq` | FIU-IND FAQ | `PMLR-8` (the STR deadline of seven working days) |
| `rbi-frm-md-2024` | RBI Master Directions on Fraud Risk Management, 15.07.2024 (HTML notification page) | `RBI-FRM-MD-2024` |

Three judgments are cited by URL only: Neelkanth (SCC Times), Kartik Chatur (SCC Times) and Shree Balaji
(Pink City Post). None of their text is fetched or stored. `sources.json` lists every URL, mirror,
licence note and extraction hint. `MANIFEST.md` records what was actually downloaded.

## What is committed, and why

Committed:

- `README.md`
- `MANIFEST.md` (provenance: URL used, bytes, sha256, retrieval time, page and text-layer status)
- `sources.json`
- `citations.json`
- `facts.json`
- `.gitignore`

`raw/` and `index/` are gitignored. The source documents are government publications with no open
licence stated, and the case reports belong to their publishers, so neither the files nor their full text
are redistributed. Only short verbatim quotes are committed:

- **`citations.json`** has exactly one entry per ID in `wapsi.citations`. Each entry has exactly the
  fields of the `Citation` model: `citation_id`, `source_title`, `section`, `quote`, `url` and `page`.
  - Each quote is verbatim and at most 25 words. Words are counted strictly, so "first-layer" counts as two.
  - A quote may join two parts of the same passage with "…". Each part must appear in order, within 600
    characters of the previous one.
  - Judgments reported by news or blog sites have an empty `quote` and a source `url`.
- **`facts.json`** holds the SOP figures used in the pitch and the SOP time limits, each with a verbatim
  snippet and page numbers:
  - money saved, reported and restored: ₹7,647 / ₹52,969 / ₹167 crore, 2.18%, April 2021 to November 2025
    (PDF page 11, SOP page 9);
  - the Annexure-V pro-rata worked example.

**Page numbers** are physical pages in the downloaded PDF. The SOP PDF starts with the two-page High Court
circular, so the SOP's own "Page N of 98" label is PDF page − 2. `facts.json` gives both numbers. HTML
sources have `page: null`.

**Quote matching.** Both the quote and the source text are normalised: Unicode NFKC, curly quotes and
dashes folded to ASCII, whitespace collapsed. If that fails, the match is retried with all whitespace
removed, because pypdf sometimes splits words ("2 nd June", "de-\nfrauded"). The checker reports which
kind of match succeeded.

## Index format

`index/chunks.jsonl` has one `DocChunk` per line with these fields:

- `chunk_id`, `doc_id`
- `section`, a display label such as `SOP Para 5(ix)`, `BNSS Section 106` or `PML Rules, Rule 2(1)(g)`
- `section_key`, such as `5(ix)`, `9.1(ii)`, `11 Process 2`, `Annexure-V Illustration 2` or `2(1)(g)`
- `section_title`, `page`, `page_end`, `text` (at most 900 characters; chunks overlap by 220)
- `citation_id` and `citation_ids`

A chunk gets a citation ID in two ways:

1. It contains that citation's verbatim quote. This is the more specific signal, so it comes first.
2. Its section key, or a parent key, is mapped in `Source.section_citations`.

Tokenization for BM25 (k1 = 1.5, b = 0.75):

- lowercases, drops stopwords and applies a light stemmer;
- normalises `s.`/`sec.`/`u/s` to `section`;
- folds BNSS, PMLA, CrPC, `A/c`, `Rs.`/`₹` and `tip-off` spellings.

## Things to know about the texts

These were checked against the downloaded files on 2026-09-16.

- **Where the forward-hold rule sits.** Para 5(x) only says money held "at any layer" can be released.
  The rule that the forward-looking hold applies only to first-layer accounts, and not to nodal, pool or
  escrow accounts, is in **Para 9.1(i)** (PDF page 26). `SOP-5-X` quotes 9.1(i), and its `section` field
  says so.
- **Nodal, pool and escrow accounts.** Para 9.1(iii) says the bank *shall hold the disputed amount* in a
  nodal, escrow or pool account under s.168 read with s.94. What it discourages is suspension or seizure
  of those accounts, and 9.1(i) rules out the forward-looking hold on them. Para 9.2(b) likewise holds the
  disputed amount in an e-commerce nodal or settlement account.
- **Two different 90-day rules.**
  - Para 10.1 counts 90 days from the bank submitting a grievance. The LEA is told within the last 15
    days, and the hold can be extended once by up to 90 days.
  - The Annexure-I notice lets the bank remove a contested hold 90 days after the amount was put on hold.
- **STR deadline.** The consolidated PML Rule 8(2) published by FIU-IND says STRs are furnished
  "promptly". The "not later than seven working days" limit comes from the FIU-IND FAQ.
- **FIU-IND circular of 02.06.2025.** No public copy was found. `FIU-CIRC-2025-06-02` quotes the SOP's
  reference to it in Para 9.1(ii).
- **Unreachable URLs.**
  - The Chhattisgarh HC mirror of the SOP fails TLS verification (incomplete certificate chain).
  - `www.indiacode.nic.in` returns 403 to scripts.
  - The RBI PDF on `rbidocs.rbi.org.in` is behind a JavaScript bot challenge.
  - The primary URLs above work.
- **Text layer.** Every page of all three PDFs has a text layer. No OCR was needed.
