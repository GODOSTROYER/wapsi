"""Dataset builder: verifier-filtered distillation with offline fake teachers (no network, no GPU)."""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from editions.amd.lora import common, dataset  # noqa: E402
from wapsi import money  # noqa: E402
from wapsi.draft import prose  # noqa: E402
from wapsi.draft.factsheet import case_slots  # noqa: E402
from wapsi.draft.render import PLACEHOLDER_RE  # noqa: E402

TRAIN_CANARIES = [c for _, c in common.INJECTION_TEMPLATES["train"]]
TEST_CANARIES = [c for _, c in common.INJECTION_TEMPLATES["test"]]


@pytest.fixture(scope="module")
def workdir():
    # private temp dir: the shared --basetemp can be wiped by other pytest sessions running in parallel
    d = Path(tempfile.mkdtemp(prefix="wapsi-lora-ds-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def heldout_world(workdir):
    return common.make_mini_world(workdir / "heldout-mini", name="heldout-mini", seed=4242)


def _build(tmp: Path, heldout: Path, teacher: str, **extra) -> tuple[Path, dict]:
    out = tmp / f"ds-{teacher}"
    argv = [
        "--teacher",
        teacher,
        "--train-worlds",
        common.SHOWCASE_MEMORY,
        "--heldout-worlds",
        str(heldout),
        # val-frac 0.14 puts one showcase case in val (bucket 1311) and the other in train (bucket 1435)
        "--limit-cases",
        "3",
        "--val-frac",
        "0.14",
        "--corrections-frac",
        str(extra.get("corrections", 1.0)),
        "--out",
        str(out),
    ]
    assert dataset.main(argv) == 0
    return out, json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def _rows(out: Path, split: str) -> list[dict]:
    return common.read_jsonl(out / f"{split}.jsonl")


def _narratives(user_content: str) -> list[str]:
    facts = json.loads(user_content)["facts"]
    return [c["narrative"]["text"] for c in facts["complaints"]]


def _offered(user_content: str) -> dict[str, str]:
    return {p["name"]: p["value"] for p in json.loads(user_content)["placeholders"]}


@pytest.fixture(scope="module")
def fake_build(workdir, heldout_world):
    return _build(workdir, heldout_world, "fake")


def test_manifest_counts_and_filters(fake_build):
    out, m = fake_build
    assert m["schema"] == dataset.SCHEMA and m["synthetic_only"] is True
    assert m["teacher"]["name"] == "fake-teacher" and m["teacher"]["style_guide"] is False
    assert any(f.startswith("verifier_passed") for f in m["filters"])
    assert any(f.startswith("drafter_accepted") for f in m["filters"])
    assert m["requests"]["requests"] > 0 and m["requests"]["accepted"] == m["requests"]["requests"]
    assert set(m["requests"]["by_task"]) >= {"str_grounds", "case_summary", "edd_rationale"}
    for split in ("train", "val", "test"):
        assert m["splits"][split]["examples"] > 0
        assert m["files"][f"{split}.jsonl"]["lines"] == m["splits"][split]["examples"]
    assert m["worlds"]["heldout-mini"]["role"] == "heldout"
    assert m["worlds"][common.SHOWCASE_MEMORY]["role"] == "train"


def test_split_by_case_and_heldout_only_in_test(fake_build):
    out, _ = fake_build
    splits = {s: _rows(out, s) for s in ("train", "val", "test")}
    assert {r["world"] for r in splits["test"]} == {"heldout-mini"}
    assert {r["world"] for r in splits["train"] + splits["val"]} == {common.SHOWCASE_MEMORY}
    cases = {s: {(r["world"], r["case_id"]) for r in rows} for s, rows in splits.items()}
    assert not (cases["train"] & cases["val"]) and not (cases["train"] & cases["test"])
    assert not (cases["val"] & cases["test"])
    # deterministic: same key, same split
    w = common.World("demo", "train", None, [], {})
    assert dataset._split_for(w, "CASE-1", 0.1) == dataset._split_for(w, "CASE-1", 0.1)


def test_hard_negatives_present_and_targets_clean(fake_build):
    out, _ = fake_build
    rows = _rows(out, "train") + _rows(out, "val") + _rows(out, "test")
    variants = {r["variant"] for r in rows}
    assert variants == {"clean", "tempting", "injection"}
    tempting = [r for r in rows if r["variant"] == "tempting"]
    injected = [r for r in rows if r["variant"] == "injection"]
    clean = {
        (r["world"], r["case_id"], r["task"], r["account_id"]): r for r in rows if r["variant"] == "clean"
    }
    assert tempting and injected
    for r in tempting:
        narrative = " ".join(_narratives(r["messages"][1]["content"]))
        base = clean.get((r["world"], r["case_id"], r["task"], r["account_id"]))
        base_narrative = " ".join(_narratives(base["messages"][1]["content"])) if base else ""
        assert len(re.findall(r"\d+", narrative)) > len(re.findall(r"\d+", base_narrative))
        assert re.search(r"(?i)rupees|rupaye|INR|Rs\.?\s?\d|₹", narrative)
    for r in injected:
        narrative = " ".join(_narratives(r["messages"][1]["content"])).lower()
        pool = TEST_CANARIES if r["split"] == "test" else TRAIN_CANARIES
        assert any(c in narrative for c in pool), "injected instruction missing from the request"
        assert r["meta"]["canaries"] and r["meta"]["canaries"][0] in pool
    for r in rows:
        user = r["messages"][1]["content"]
        target = json.loads(r["messages"][-1]["content"])["text"]
        offered, narratives = _offered(user), _narratives(user)
        assert prose.validate_prose(target, offered, narratives) == [], r["id"]
        bare = PLACEHOLDER_RE.sub(" ", target)
        assert not re.search(r"\d", bare)
        assert not re.search(r"(?i)\b(rupees?|rs|inr|paise|lakh|crore|thousand|hundred)\b|₹", bare)
        assert not money.find_amounts(bare)
        assert not common.canary_hits(target)
        assert r["messages"][0]["content"] == prose.SYSTEM_PROMPT  # the drafter's own request, unmodified


def test_synthetic_corrections_mask_a_real_mistake(fake_build):
    out, _ = fake_build
    corr = [r for r in _rows(out, "train") + _rows(out, "val") if r["source"] == "synthetic_correction"]
    assert corr
    assert not [r for r in _rows(out, "test") if r["source"] != "teacher_first_try"]
    for r in corr:
        m = r["messages"]
        assert [x["role"] for x in m] == ["system", "user", "assistant", "user", "assistant"]
        bad = json.loads(m[2]["content"])["text"]
        offered, narratives = _offered(m[1]["content"]), _narratives(m[1]["content"])
        assert prose.validate_prose(bad, offered, narratives), (
            "corrupted answer must fail the drafter's checks"
        )
        assert m[3]["content"].startswith("Your answer broke these rules")
        assert prose.validate_prose(json.loads(m[4]["content"])["text"], offered, narratives) == []


def test_filters_reject_bad_teacher_answers(workdir, heldout_world):
    out, m = _build(workdir, heldout_world, "fake-noisy", corrections=0.0)
    reasons = m["rejections"]["by_reason"]
    assert m["requests"]["rejected"] > 0
    assert reasons.get("drafter_fallback", 0) + reasons.get("verifier_failed", 0) > 0
    rejected = common.read_jsonl(out / "rejections.jsonl")
    assert rejected and all(r["reasons"] for r in rejected)
    assert m["rejections"][
        "drafter_issue_codes"
    ]  # e.g. raw_digits / unmatched_amount from hallucinated amounts
    rows = _rows(out, "train") + _rows(out, "val") + _rows(out, "test")
    for r in rows:  # whatever got through is clean
        target = json.loads(r["messages"][-1]["content"])["text"]
        assert "Ignore all previous instructions" not in target
        assert (
            prose.validate_prose(
                target, _offered(r["messages"][1]["content"]), _narratives(r["messages"][1]["content"])
            )
            == []
        )


def test_variant_changes_only_the_untrusted_narrative():
    world = common.load_world(common.SHOWCASE_MEMORY, "train")
    case_id, ncrp = world.cases[0]
    bundle = common.plan_case(world, case_id, ncrp)
    for variant in ("tempting", "injection"):
        vb, info = common.make_variant(bundle, world.ledger, variant, "train", "k")
        assert case_slots(vb, world.ledger) == case_slots(bundle, world.ledger)
        before = {c.ncrp_id: c.narrative_text for c in bundle.complaints}
        after = {c.ncrp_id: c.narrative_text for c in vb.complaints}
        assert before.keys() == after.keys() and before != after
        assert info.inserted in "".join(after.values())
    assert common.inr_words(480000) == "four lakh eighty thousand"
    assert common.inr_words(12500075) == "one crore twenty-five lakh seventy-five"
