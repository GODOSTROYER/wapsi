"""AMD ACT III kickoff automation (editions/amd/kickoff): scripts parse, plans are deterministic, guards hold,
the evidence bundle and the pitch filler work on synthetic fixtures, and nothing claims a GPU number.

Offline: no GPU, no Docker, no network. Every "measurement" below is a hand-written synthetic fixture value.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from editions.amd.kickoff import collect_evidence, fill_pitch, stagelog  # noqa: E402

KICK = ROOT / "editions" / "amd" / "kickoff"
STAGES = ["env", "models", "gnn", "lora-data", "lora-train", "merge", "lora-eval", "serve", "eval", "bench", "ab",
          "evidence"]
SECRET = "hf_TESTONLY_never_print_this_0123456789"
API_SECRET = "vllm-TESTONLY-key-abcdef"


def _bash_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("WAPSI_", "VLLM_", "HF_", "AMLWORLD", "ROCM_PYTORCH", "SCRATCH", "RADEON_"))}
    env.update({"HF_TOKEN": SECRET, "VLLM_API_KEY": API_SECRET, "NO_COLOR": "1"})
    env.update(extra)
    return env


def _bash(bash: str, *args: str, env: dict[str, str] | None = None, timeout: float = 120,
          attempts: int = 1) -> subprocess.CompletedProcess:
    """Run Git Bash / bash with stdin closed and a bound on the time.

    Git Bash (MSYS2) start-up is the fragile part on a loaded Windows machine: it inherits the test process's stdin
    handle (a pipe under some runners, which MSYS probes at start-up) and emulates fork, which can fail transiently
    (DLL init, address-space clashes) while the full suite runs other processes. So stdin is /dev/null, and with
    ``attempts`` > 1 a start-up that timed out or died without bash's own diagnostics is retried. A real error (bash
    printed a "syntax error", or the script's own messages) is returned at once."""
    res = None
    for attempt in range(attempts):
        try:
            res = subprocess.run([bash, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", env=env, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            if attempt == attempts - 1:
                raise
            continue
        startup_failure = res.returncode not in (0, 1, 2) and not res.stdout and "syntax error" not in res.stderr
        if not startup_failure:
            return res
    assert res is not None
    return res


def _run(bash: str, *args: str, env: dict[str, str] | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return _bash(bash, str(KICK / "run_all.sh"), *args, env=env or _bash_env(), timeout=timeout)


# --------------------------------------------------------------------------- shell scripts


@pytest.mark.parametrize("script", ["run_all.sh", "preflight.sh"])
def test_shell_scripts_parse(bash, script):
    # `bash -n` reads nothing but the script. This test once stopped a full-suite run and passed alone: a Git Bash
    # start-up problem under load, not a parse error (a parse error prints "syntax error" and exits 2). stdin is
    # closed, the run is bounded, and a start-up that timed out or died silently is retried once.
    res = _bash(bash, "-n", str(KICK / script), timeout=60, attempts=2)
    assert res.returncode == 0, (res.returncode, res.stderr)


def test_list_and_argument_errors(bash):
    res = _run(bash, "--list")
    assert res.returncode == 0
    names = [line.split()[1] for line in res.stdout.splitlines() if line.strip()]
    assert names == STAGES
    assert _run(bash, "--dry-run", "--stage", "nope").returncode == 2
    assert _run(bash, "--dry-run", "--stage", "bench", "--from", "serve").returncode == 2
    assert _run(bash, "--dry-run", "--date", "12-10-2026").returncode == 2
    assert _run(bash, "--dry-run", "--mode", "cluster").returncode == 2
    # GPU evidence must stay inside the repository (the training containers write it through /work)
    assert _run(bash, "--dry-run", "--mode", "droplet", "--evidence-root", "/tmp/elsewhere").returncode == 2


def test_gpu_dry_run_plan_is_deterministic_and_complete(bash):
    a = _run(bash, "--mode", "droplet", "--dry-run", "--force", "--date", "2026-10-12")
    b = _run(bash, "--mode", "gpu", "--dry-run", "--force", "--date", "2026-10-12")  # gpu: the droplet's old name
    assert a.returncode == 0, a.stderr
    assert a.stdout == b.stdout  # same inputs, same plan
    out = a.stdout
    headers = [line for line in out.splitlines() if line.startswith("== [")]
    assert [h.split("] ")[1].split(":")[0] for h in headers] == STAGES
    for needle in (
        "editions/amd/evidence/2026-10-12",
        "preflight.sh --mode droplet --json",
        "model_plan.py --mode droplet --detect-vram",
        "editions.amd.kickoff.golden_eval --llm-url http://127.0.0.1:8000/v1",
        "stagelog.py static",
        "pull_and_pin.sh",
        "uv sync --frozen --no-dev --extra docs",
        "wapsi.search.fetch",
        "wapsi.search.build",
        "train_image_check --require-gpu",
        "hf download google/gemma-4-31B-it",
        "hf download google/gemma-4-E4B-it",
        "editions.amd.gnn.train",
        "--device cuda",
        "editions.amd.lora.dataset --teacher",
        "editions.amd.lora.train --config gemma4-31b-mi300x",
        "editions.amd.lora.merge --run /wk/lora/run --out /models/wapsi-gemma-4-31b-str-merged",
        "editions.amd.lora.evaluate --base",
        "docker compose -f editions/amd/docker-compose.yml up -d llm",
        "gpu_telemetry.py --interval 2",
        "--concurrency 1 4 16 64",
        "--name e2e_c16",
        "editions.amd.kickoff.ab_hallucination --llm-url",
        "collect_evidence.py --evidence-dir editions/amd/evidence/2026-10-12 --mode droplet",
        "fill_pitch.py --evidence",
        "warn 75%, stop 90%",
        "(dry run: nothing was executed)",
    ):
        assert needle in out, needle
    assert SECRET not in out and API_SECRET not in out  # secrets are never printed
    assert "-e HF_TOKEN " in out  # passed to containers by name only


def test_rehearsal_dry_run_is_deterministic_and_gpu_free(bash):
    a = _run(bash, "--rehearsal", "--dry-run", "--force", "--date", "2026-10-07")
    b = _run(bash, "--rehearsal", "--dry-run", "--force", "--date", "2026-10-07")
    assert a.returncode == 0, a.stderr
    assert a.stdout == b.stdout
    out = a.stdout
    assert "editions/amd/evidence/cpu-rehearsal/2026-10-07" in out
    assert "NOT an MI300X result" in out
    commands = [line for line in out.splitlines() if line.startswith("  $")]
    assert commands
    commands = " ".join(commands)
    for forbidden in ("docker ", "hf download", "--device cuda", "amd-smi metric", "vllm"):
        assert forbidden not in commands, forbidden
    for needle in ("editions.amd.kickoff.rehearsal worlds", "--config tiny-cpu", "editions.amd.lora.mock_endpoint",
                   "editions.amd.gnn.service", "--mode rehearsal", "train_image_check --device cpu",
                   "editions.amd.kickoff.golden_eval --fake", "fill_pitch.py must refuse rehearsal evidence"):
        assert needle in out, needle


def test_only_and_from_select_stages(bash):
    out = _run(bash, "--mode", "droplet", "--dry-run", "--force", "--date", "2026-10-12", "--stage", "bench,ab").stdout
    assert [line.split("] ")[1].split(":")[0] for line in out.splitlines() if line.startswith("== [")] == ["bench", "ab"]
    out = _run(bash, "--mode", "droplet", "--dry-run", "--force", "--date", "2026-10-12", "--from", "serve").stdout
    assert [line.split("] ")[1].split(":")[0] for line in out.splitlines() if line.startswith("== [")] == [
        "serve", "eval", "bench", "ab", "evidence"]


@pytest.mark.parametrize("now,needle", [("2026-10-07T12:00:00Z", "before kickoff"),
                                        ("2026-10-19T09:00:00Z", "event window has closed")])
@pytest.mark.parametrize("mode", ["droplet", "notebook", "endpoint"])
def test_gpu_run_refused_outside_the_event_window(bash, now, needle, mode):
    res = _run(bash, "--mode", mode, "--date", "2099-01-01", env=_bash_env(WAPSI_NOW=now))
    assert res.returncode == 2
    assert needle in res.stderr
    for sub in ("", "notebook", "endpoint"):  # refused before writing anything
        assert not (ROOT / "editions" / "amd" / "evidence" / sub / "2099-01-01").exists()


def test_preflight_table_never_prints_the_token(bash, tmp_path):
    env = _bash_env()
    env["SCRATCH"] = (tmp_path / "no-such-mount").as_posix()  # a missing SCRATCH is FAIL, never the root disk
    res = _bash(bash, str(KICK / "preflight.sh"), "--mode", "droplet", "--offline", "--json",
                (tmp_path / "pf.json").as_posix(), env=env)
    assert "CHECK" in res.stdout and "HF_TOKEN" in res.stdout
    assert SECRET not in res.stdout and SECRET not in res.stderr
    assert "set in this shell (value not shown)" in res.stdout
    data = json.loads((tmp_path / "pf.json").read_text(encoding="utf-8"))
    assert data["mode"] == "droplet" and any(c["check"] == "HF_TOKEN" and c["status"] == "PASS" for c in data["checks"])
    disk = next(c for c in data["checks"] if c["check"].startswith("disk (SCRATCH="))
    assert disk["status"] == "FAIL" and "does not exist" in disk["detail"], disk
    assert any(c["check"] == "docs corpus" for c in data["checks"])
    if not shutil.which("amd-smi") and not shutil.which("rocm-smi"):
        assert res.returncode == 1 and data["fails"] >= 1  # a laptop is not the droplet
    assert "network to huggingface.co" in res.stdout and "--offline" in res.stdout


# --------------------------------------------------------------------------- stagelog: window, budget, records


def test_event_window_boundaries():
    p = stagelog.parse_ts
    assert stagelog.window_state(p("2026-10-12T14:59:59Z")) == "before"
    assert stagelog.window_state(p("2026-10-12T15:00:00Z")) == "inside"
    assert stagelog.window_state(p("2026-10-18T14:00:00Z")) == "inside"
    assert stagelog.window_state(p("2026-10-18T14:00:01Z")) == "after"


@pytest.mark.parametrize("spent,state,code", [(50, "ok", 0), (60, "warn", 10), (75, "stop", 20)])
def test_budget_guard(tmp_path, monkeypatch, capsys, spent, state, code):
    monkeypatch.setenv("WAPSI_NOW", "2026-10-13T20:00:00Z")
    monkeypatch.setenv("WAPSI_DROPLET_STARTED_AT", "2026-10-13T10:00:00Z")  # 10 h x $1.99 = $19.90
    monkeypatch.setenv("WAPSI_SPENT_USD", str(spent))
    for k in ("WAPSI_GPU_HOUR_USD", "WAPSI_BUDGET_USD", "WAPSI_BUDGET_WARN_PCT", "WAPSI_BUDGET_STOP_PCT"):
        monkeypatch.delenv(k, raising=False)
    b = stagelog.budget(tmp_path, "gpu")
    assert b["hours"] == 10.0 and b["droplet_usd"] == 19.9 and b["total_usd"] == round(spent + 19.9, 2)
    assert b["state"] == state and b["billed"] is True
    assert stagelog.main(["budget", "--evid", str(tmp_path), "--mode", "gpu", "--stage", "bench"]) == code
    out = capsys.readouterr().out
    if state != "ok":
        assert "DESTROY" in out
    if state == "stop":
        assert "Not starting stage 'bench'" in out


def test_budget_start_falls_back_to_first_run_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("WAPSI_DROPLET_STARTED_AT", raising=False)
    monkeypatch.setenv("WAPSI_NOW", "2026-10-07T10:00:00Z")
    start, source = stagelog.budget_start(tmp_path, "rehearsal")
    assert source == "first run_all.sh run" and stagelog.iso(start) == "2026-10-07T10:00:00Z"
    monkeypatch.setenv("WAPSI_NOW", "2026-10-07T12:30:00Z")
    b = stagelog.budget(tmp_path, "rehearsal")
    assert b["hours"] == 2.5 and b["billed"] is False and "hypothetical" in b["label"]


def test_boot_time_from_proc_stat(tmp_path):
    stat = tmp_path / "stat"
    stat.write_text("cpu  1 2 3\nbtime 1792000000\nprocesses 5\n", encoding="utf-8")
    assert stagelog.boot_time(stat).timestamp() == 1792000000
    assert stagelog.boot_time(tmp_path / "missing") is None


def test_notebook_budget_never_counts_from_the_shared_hosts_boot_time(tmp_path, monkeypatch):
    """Seen on 2026-10-14: a notebook container reports its host's boot time, 12 days before the session."""
    monkeypatch.delenv("WAPSI_DROPLET_STARTED_AT", raising=False)
    monkeypatch.setenv("WAPSI_GPU_HOUR_USD", "1.99")
    monkeypatch.setenv("WAPSI_NOW", "2026-10-14T15:00:00Z")
    stat = tmp_path / "stat"
    stat.write_text(f"btime {int(stagelog.parse_ts('2026-10-02T14:47:52Z').timestamp())}\n", encoding="utf-8")
    assert stagelog.budget(tmp_path / "d", "droplet", proc_stat=stat)["started_source"].startswith("boot time")
    nb = stagelog.budget(tmp_path / "n", "notebook", proc_stat=stat)
    assert nb["started_source"] == "first run_all.sh run" and nb["hours"] == 0.0
    # an older notebook bundle whose stages counted from the boot time: the total is not printed as a cost
    old = {**nb, "started_source": "boot time (/proc/stat btime)", "total_usd": 574.29}
    md = collect_evidence.to_markdown({**collect_evidence.build(make_evidence(tmp_path / "ev"), "notebook"),
                                       "budget": old})
    assert "Budget: not shown" in md and "of $100" not in md


def test_stage_record_and_notes(tmp_path):
    notes = tmp_path / "n.notes"
    notes.write_text("git_commit=abc123\n# comment\nserved_profile=gemma4-merged\n", encoding="utf-8")
    out = stagelog.record(tmp_path, index=3, stage="gnn", mode="gpu", status="ok", exit_code=0,
                          started="2026-10-12T15:10:00Z", ended="2026-10-12T15:40:30Z", log="logs/03-gnn.log",
                          notes=notes, budget_info={"total_usd": 1.0})
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert out.name == "03-gnn.json"
    assert rec["duration_s"] == 1830.0 and rec["window"] == "inside" and rec["label"] == "MI300X run"
    assert rec["notes"] == {"git_commit": "abc123", "served_profile": "gemma4-merged"}
    rec2 = json.loads(stagelog.record(tmp_path, index=1, stage="env", mode="rehearsal", status="failed", exit_code=1,
                                      started="2026-10-07T10:00:00Z", ended="2026-10-07T10:00:05Z").read_text())
    assert "not a GPU result" in rec2["label"] and rec2["window"] == "before"


def _fixture_runner(bench_fixtures: Path):
    files = {("amd-smi", "metric"): "amd_smi_metric.json", ("amd-smi", "process"): "amd_smi_process.json",
             ("amd-smi", "static"): "amd_smi_static.json"}

    def runner(cmd: list[str]) -> str | None:
        name = files.get(tuple(cmd[:2]))
        return (bench_fixtures / name).read_text(encoding="utf-8") if name else None

    return runner


def test_snapshot_and_static_from_fixtures(bench_fixtures, tmp_path):
    runner = _fixture_runner(bench_fixtures)
    snap = stagelog.snapshot(runner, proc_root=tmp_path)
    assert snap["available"] is True and snap["source"] == "amd-smi" and snap["gpus"]
    info = stagelog.static_info(runner)
    assert info["available"] and info["gpus"][0]["name"] == "AMD Instinct MI300X"
    none = stagelog.snapshot(lambda cmd: None, proc_root=tmp_path)
    assert none["available"] is False and "no AMD GPU" in none["reason"]
    assert stagelog.static_info(lambda cmd: None)["available"] is False


# --------------------------------------------------------------------------- synthetic evidence fixture

FIXTURE_NOTE = "synthetic test fixture written by tests/amd/test_amd_kickoff.py; not a measurement"


def _w(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_fixture": FIXTURE_NOTE, **obj}, indent=1), encoding="utf-8")


def _levels(values: dict[int, float]) -> list[dict]:
    return [{"concurrency": c, "requests": 8, "completed": 8, "output_tokens_per_s": v,
             "ttft_s": {"p50": 0.1 * c, "p95": 0.2 * c}, "e2e_s": {"p95": 1.5 * c}, "requests_per_s": c / 2}
            for c, v in values.items()]


def make_evidence(root: Path, *, started: str = "2026-10-12T15:30:00Z", gpu: str = "AMD Instinct MI300X",
                  date: str = "2026-10-12", static: bool = True) -> Path:
    evid = root / date
    if static:
        _w(evid / "amd_smi_static.json", {"available": True, "gpus": [{"gpu": 0, "name": gpu, "sriov_vf": False}],
                                          "rocm_version": "7.1.0"})
    for i, name in enumerate(["env", "bench"], start=1):
        _w(evid / "stages" / f"0{i}-{name}.json",
           {"stage": name, "index": i, "status": "ok", "started_at": started, "ended_at": started, "duration_s": 1.0,
            "notes": {"git_commit": "f1x7ure"} if i == 1 else {}, "budget": {"total_usd": 12.5, "label": "fixture"}})
    _w(evid / "bench" / "llm_bench.json", {"kind": "llm_bench", "model": "wapsi-llm",
                                           "levels": _levels({1: 50.0, 4: 180.0, 16: 600.0, 64: 1500.4})})
    _w(evid / "bench" / "llm_bench_verifier.json", {"kind": "llm_bench", "model": "wapsi-verifier",
                                                    "levels": _levels({1: 90.0, 4: 300.0, 16: 900.0})})
    for c, cph in ((1, 100.0), (8, 600.0), (16, 900.0)):
        _w(evid / "bench" / f"e2e_c{c}.json", {
            "kind": "e2e_bench", "world": "demo", "settings": {"concurrency": c}, "model": {"llm": "vllm-rocm:wapsi-llm"},
            "metrics": {"cases": 39, "cases_ok": 39, "cases_per_hour": cph, "wall_s": 39 / cph * 3600, "drafts": 390,
                        "time_to_hold_plan_s": {"p50": 0.5}, "case_total_s": {"p95": 9.0},
                        "draft_verification_pass_rate": 1.0, "prose_fallback_rate": 0.05, "hold_exact_match_rate": 1.0},
            "cost": {"gpu_hour_usd": 1.99, "usd_per_case": round(1.99 / cph, 8), "inr_per_usd": 84.0,
                     "inr_per_case": round(1.99 / cph * 84.0, 6)}})
    _w(evid / "bench" / "telemetry_summary.json", {
        "source": "amd-smi", "samples": 10, "interval_s": 2,
        "gpus": [{"gpu": 0, "name": gpu, "vram_total_mb": 196608, "vram_used_mb_peak": 140000.0}],
        "processes": [{"owner": "wapsi-amd-llm-1", "vram_mb_peak": 105000.0, "process_names": ["python3"]},
                      {"owner": "wapsi-amd-verifier-llm-1", "vram_mb_peak": 29000.0, "process_names": ["python3"]},
                      {"owner": "wapsi-amd-gnn-1", "vram_mb_peak": 2048.0, "process_names": ["python3"]}]})
    _w(evid / "gnn_metrics.json", {"best_epoch": 3, "train_s": 40.0, "history": [
        {"epoch": 1, "epoch_s": 9.0, "edges_per_s": 1000.0, "peak_mem_mb": 2048.0, "val_auc": 0.9},
        {"epoch": 2, "epoch_s": 1.0, "edges_per_s": 250000.0, "peak_mem_mb": 4096.0, "val_auc": 0.95},
        {"epoch": 3, "epoch_s": 2.0, "edges_per_s": 150000.0, "peak_mem_mb": 4096.0, "val_auc": 0.97}],
        "test": {"pooled": {"seed": {"auc": 0.96}}}})
    _w(evid / "gnn_hard_eval.json", {"world": "hard", "cases": 64, "by_scorer": {
        "heuristic-v2": {"disposition_accuracy": 0.9198},
        "gnn:mi300x-base": {"disposition_accuracy": 0.93, "role_recall": {"INNOCENT_HOLDER": 0.9},
                            "classification": {"mule_auc": 0.985}}}})
    _w(evid / "lora_eval.json", {"cases": 40, "models": {
        "base": {"overall": {"prose_verifier_pass_rate": 0.8, "fallback_rate": 0.12, "acceptance_rate": 0.7}},
        "tuned": {"overall": {"prose_verifier_pass_rate": 0.97, "fallback_rate": 0.02, "acceptance_rate": 0.95}}}})
    _w(evid / "lora_train_metrics.json", {"tokens_per_s": {"mean": 3456.7}, "peak_mem_gb": 88.4,
                                          "train_runtime_s": 3600.0})
    _w(evid / "ab_hallucination.json", {"label": "fixture", "cases": 20, "arms": {
        "raw": {"docs": 20, "docs_with_wrong_amount": 7, "invented_accounts": 4, "unknown_citations": 9,
                "verifier_pass_rate": 0.45, "clean_rate": 0.45},
        "bound": {"docs": 180, "docs_with_wrong_amount": 0, "invented_accounts": 0, "unknown_citations": 0,
                  "verifier_pass_rate": 1.0, "clean_rate": 1.0, "prose_fallbacks": 6,
                  "str_only": {"docs": 20, "docs_with_wrong_amount": 0, "invented_accounts": 0, "unknown_citations": 0,
                               "verifier_pass_rate": 1.0, "clean_rate": 1.0}}}})
    return evid


def test_collect_evidence_on_fixture(tmp_path, capsys):
    evid = make_evidence(tmp_path)
    assert collect_evidence.main(["--evidence-dir", str(evid), "--mode", "gpu", "--quiet"]) == 0
    ev = json.loads((evid / "evidence.json").read_text(encoding="utf-8"))
    h = ev["headline"]
    assert ev["mode"] == "droplet" and ev["gpu_results_valid"] is True and ev["invalid_reasons"] == []
    assert ev["results_valid"] is True and ev["compute"]["short"] == "MI300X"
    assert ev["compute"]["kind"] == "amd-developer-cloud-droplet"
    assert h["measured_date"] == "2026-10-12" and h["gpu_name"] == "AMD Instinct MI300X"
    assert (h["llm_tok_s_c1"], h["llm_tok_s_c64"], h["llm_peak_tok_s"]) == (50.0, 1500.4, 1500.4)
    assert h["llm_ttft_p95_s_c16"] == 3.2 and h["llm_e2e_p95_s_c16"] == 24.0
    assert h["verifier_tok_s_c16"] == 900.0
    assert h["e2e_cases_per_hour_c16"] == 900.0 and h["e2e_best_concurrency"] == 16
    assert h["e2e_best_drafts_per_gpu_hour"] == 9000.0  # 390 drafts / (39/900 h): derived, not typed in
    assert h["e2e_best_usd_per_case"] == pytest.approx(1.99 / 900, abs=1e-6)
    assert h["e2e_best_inr_per_case"] == pytest.approx(1.99 / 900 * 84, abs=1e-6)
    assert (h["vram_llm_gb"], h["vram_verifier_gb"], h["vram_gnn_gb"]) == (102.5, 28.3, 2.0)
    assert h["gnn_epoch_s"] == 1.5 and h["gnn_val_auc"] == 0.97 and h["gnn_peak_mem_gb"] == 4.0
    assert (h["gnn_hard_disposition"], h["gnn_hard_mule_auc"], h["gnn_hard_ih_recall"]) == (0.93, 0.985, 0.9)
    assert h["lora_base_prose_verifier_pass_rate"] == 0.8 and h["lora_tuned_fallback_rate"] == 0.02
    assert (h["ab_raw_docs_with_wrong_amount"], h["ab_bound_docs_with_wrong_amount"]) == (7, 0)
    assert (h["ab_raw_docs"], h["ab_bound_str_docs"], h["ab_bound_docs"]) == (20, 20, 180)  # like with like
    assert ev["sources"]["ab_bound_str_docs"] == "ab_hallucination.json: arms.bound.str_only.docs"
    assert ev["sources"]["llm_tok_s_c16"].startswith("bench/llm_bench.json")
    assert ev["warnings"] == [] and ev["hardware"]["gpu_name_source"] == "amd_smi_static.json"
    md = (evid / "evidence.md").read_text(encoding="utf-8")
    assert md.startswith("# Wapsi on AMD Instinct MI300X: evidence, 2026-10-12")
    assert "AMD Instinct MI300X" in md and "| Drafts per GPU-hour (best concurrency) | 9,000 |" in md
    assert "CPU REHEARSAL" not in md


def test_collect_evidence_flags_runs_outside_the_window_and_rehearsals(tmp_path):
    early = collect_evidence.build(make_evidence(tmp_path / "a", started="2026-10-11T09:00:00Z"), "gpu")
    assert early["gpu_results_valid"] is False
    assert any("outside the event window" in r for r in early["invalid_reasons"])
    other_gpu = collect_evidence.build(make_evidence(tmp_path / "b", gpu="Some Other GPU"), "gpu")
    assert other_gpu["gpu_results_valid"] is False
    reh = collect_evidence.build(make_evidence(tmp_path / "c"), "rehearsal")
    assert reh["mode"] == "cpu-rehearsal" and reh["gpu_results_valid"] is False
    assert "CPU REHEARSAL" in reh["label"] and "NOT GPU results" in reh["label"]
    assert collect_evidence.to_markdown(reh).splitlines()[2].startswith("> **CPU REHEARSAL")


# --------------------------------------------------------------------------- fill_pitch


def _checker():
    spec = importlib.util.spec_from_file_location("pitch_check_numbers_kickoff", ROOT / "pitch" / "check_numbers.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Figures a bundle carries that the pitch does not use today: a page an author could add (TBD(id) + a row in the
# open-slots table of NUMBERS.md), so the tests cover the optional slots next to the real deck's own. The last
# line is a slot that is filled by hand, never by a bundle.
EXTRA_PAGE = """
## Measured on the GPU box

Measured on TBD(gpu_where). Time to first token p50 / p95 at concurrency 16: TBD(gpu_ttft). Verifier tokens/s:
TBD(gpu_verifier_tps). Cost per case: TBD(gpu_cost_case). Memory: TBD(gpu_vram_peak). LoRA: TBD(gpu_lora).
The GNN on the hard world: TBD(gpu_gnn_hard). STR against STR (TBD(ab_where)): TBD(ab_str_wrong).

By hand: TBD(evolus_blocks).
"""
EXTRA_IDS = ("gpu_where", "gpu_ttft", "gpu_verifier_tps", "gpu_cost_case", "gpu_vram_peak", "gpu_lora", "gpu_gnn_hard",
             "ab_where", "ab_str_wrong")
HAND_FILLED = "evolus_blocks"  # in fill_pitch.MANUAL_SLOTS: no bundle may ever fill it
# Token Factory figures the real pitch now carries filled by hand from a harness report (fill_pitch.EXTRA_SLOTS); the
# copy keeps them as TBD(id) on the deck so the endpoint fill path (Gemma-only, endpoint-only wording) stays tested
TF_IDS = ("tf_demo_pass", "tf_demo_fallback", "tf_demo_hold_exact", "ab_raw_wrong", "tf_call_latency")
_FILLED = re.compile(r"<!-- slot:([a-z0-9_]+) -->.*?<!-- /slot -->", re.S)


def _pitch_copy(tmp_path: Path, extra_page: str = EXTRA_PAGE, extra_ids: tuple[str, ...] = EXTRA_IDS) -> Path:
    """The real deck, video script and NUMBERS.md as they are before any run, plus ``extra_page``.

    The tests must not depend on how far the pitch has been filled during the event week, so the copy is put back
    into its open state: a slot a real run has filled is ``TBD(id)`` again, the generated NUMBERS.md section is
    gone, every slot the pitch owns is on the deck at least once, and every id used is listed as an open slot."""
    pitch = tmp_path / "pitch"
    (pitch / "amd").mkdir(parents=True)
    for rel in ("NUMBERS.md", "amd/DECK_OUTLINE.md", "amd/VIDEO_SCRIPT.md"):
        text = _FILLED.sub(lambda m: f"TBD({m.group(1)})", (ROOT / "pitch" / rel).read_text(encoding="utf-8"))
        (pitch / rel).write_text(text, encoding="utf-8", newline="\n")
    deck = pitch / "amd" / "DECK_OUTLINE.md"
    text = deck.read_text(encoding="utf-8")
    own = [sid for sid in (*(s.sid for s in fill_pitch.SLOTS), *TF_IDS)
           if f"TBD({sid})" not in text]  # reworded out of the deck (or filled by hand) since
    text += "".join(f"\n{sid}: TBD({sid}).\n" for sid in own) + extra_page
    deck.write_text(text, encoding="utf-8", newline="\n")
    numbers = pitch / "NUMBERS.md"
    text = numbers.read_text(encoding="utf-8")
    if fill_pitch.END in text:  # a real run has been merged: drop its generated section from the copy
        start = text.index(fill_pitch.BEGIN.split(" (")[0])
        text = text[:start] + text[text.index(fill_pitch.END) + len(fill_pitch.END):]
    assert "| Slot |" in text and text.rstrip().splitlines()[-1].startswith("|"), "the open-slots table ends the page"
    listed = set(_checker().parse_slots(numbers))
    wanted = [*(s.sid for s in fill_pitch.SLOTS), *TF_IDS, HAND_FILLED, *extra_ids]
    rows = "".join(f"| {sid} | a figure not measured yet | as measured | a results file |\n"
                   for sid in dict.fromkeys(wanted) if sid not in listed)
    numbers.write_text(text.rstrip("\n") + "\n" + rows, encoding="utf-8", newline="\n")
    return pitch


def _fixture_bundle(tmp_path: Path) -> Path:
    evid = make_evidence(tmp_path / "ev")
    ev = collect_evidence.build(evid, "gpu")
    ev["synthetic_fixture"] = True
    path = evid / "evidence.json"
    path.write_text(json.dumps(ev), encoding="utf-8")
    return path


def test_the_real_pitch_and_fill_pitch_agree_on_the_slots_and_stay_honest():
    """Every TBD(id) of the pitch is a slot fill_pitch can fill or a documented hand-filled one, and is listed in the
    open-slots table of NUMBERS.md; nothing is filled except from a valid evidence bundle."""
    check = _checker()
    listed = set(check.parse_slots(ROOT / "pitch" / "NUMBERS.md"))
    texts = fill_pitch.pitch_texts(ROOT / "pitch")
    open_ids = fill_pitch.open_slot_ids(texts) - {"slot_id"}  # "TBD(slot_id)" is the notation, explained in prose
    fillable, manual = {s.sid for s in fill_pitch.ALL_SLOTS}, set(fill_pitch.MANUAL_SLOTS)
    assert len(fillable) == len(fill_pitch.ALL_SLOTS) and not fillable & manual
    assert open_ids <= fillable | manual, sorted(open_ids - fillable - manual)
    assert open_ids <= listed, sorted(open_ids - listed)
    assert listed <= fillable | manual, sorted(listed - fillable - manual)
    for slot in fill_pitch.SLOTS:  # the ids fill_pitch calls "the pitch's" really are in the pitch (open or filled)
        assert fill_pitch.find(slot, texts), slot.sid
    numbers = (ROOT / "pitch" / "NUMBERS.md").read_text(encoding="utf-8")
    filled_any = any(fill_pitch._marker(s.sid).search(t) for s in fill_pitch.ALL_SLOTS for t in texts.values())
    if filled_any:  # after a run: the generated NUMBERS.md section must point at valid evidence bundles
        assert fill_pitch.END in numbers
        sources = {line.split("|")[4].strip() for line in numbers.splitlines() if line.startswith("| amd_")}
        assert sources
        for src in sources:  # every bundle a row re-reads (rows from several later-week runs are merged)
            ev = json.loads((ROOT / src).read_text(encoding="utf-8"))
            assert ev["mode"] in ("droplet", "notebook", "endpoint", "gpu"), src
            assert ev.get("results_valid", ev.get("gpu_results_valid")) is True and not ev.get("synthetic_fixture"), src
    else:
        assert fill_pitch.END not in numbers
    # what a slot may claim: a Token Factory figure is an endpoint run of Gemma, a droplet figure an MI300X run
    by_id = {s.sid: s for s in fill_pitch.ALL_SLOTS}
    for sid in ("tf_demo_pass", "tf_demo_fallback", "tf_demo_hold_exact", "tf_call_latency", "ab_raw_wrong"):
        assert by_id[sid].modes == ("endpoint",) and by_id[sid].gemma, sid
    for sid in ("droplet_tokens_s", "droplet_cases_hour", "droplet_vram"):
        assert by_id[sid].mi300x and "endpoint" not in by_id[sid].modes, sid
    assert all("endpoint" not in by_id[sid].modes for sid in ("gnn_amd_auc", "gnn_amd_epoch"))


def test_fill_pitch_dry_run_then_write_then_refill(tmp_path, capsys):
    pitch, bundle = _pitch_copy(tmp_path), _fixture_bundle(tmp_path)
    before = {p: p.read_text(encoding="utf-8") for p in pitch.rglob("*.md")}
    args = ["--evidence", str(bundle), "--pitch-dir", str(pitch), "--allow-fixture", "--strict"]
    assert fill_pitch.main(args) == 0
    out = capsys.readouterr().out
    assert "+++ b/pitch/amd/DECK_OUTLINE.md" in out and "dry run" in out
    assert {p: p.read_text(encoding="utf-8") for p in pitch.rglob("*.md")} == before  # dry run touches nothing

    assert fill_pitch.main([*args, "--write"]) == 0
    out = capsys.readouterr().out
    deck = (pitch / "amd" / "DECK_OUTLINE.md").read_text(encoding="utf-8")
    video = (pitch / "amd" / "VIDEO_SCRIPT.md").read_text(encoding="utf-8")
    # the deck's own slots, filled where the words fit an MI300X run (the sentence around each stays the author's)
    assert "<!-- slot:droplet_tokens_s -->50 / 180 / 600 tokens/s at concurrency 1 / 4 / 16<!-- /slot -->" in deck
    assert "<!-- slot:droplet_vram -->102.5 GB / 28.3 GB / 2.0 GB (drafting model / verifier / GNN)<!-- /slot -->" in deck
    assert "<!-- slot:droplet_cases_hour -->900<!-- /slot -->" in deck
    assert "<!-- slot:gnn_amd_auc -->0.985 (MI300X)<!-- /slot -->" in deck and "<!-- slot:gnn_amd_epoch -->1.50 s" in deck
    assert "<!-- slot:ab_bound_wrong -->0 (of 180 drafts)<!-- /slot -->" in deck
    for page in (deck, video):  # every place of a slot, in every file
        assert "TBD(gnn_amd_auc)" not in page and "TBD(ab_bound_wrong)" not in page
    # the optional slots of the extra page
    assert "Measured on <!-- slot:gpu_where -->MI300X, 2026-10-12<!-- /slot -->." in deck
    assert "<!-- slot:gpu_cost_case -->$0.0022 (₹0.19)<!-- /slot -->" in deck
    assert "<!-- slot:gpu_lora -->pass 0.8 to 0.97, fallback 0.12 to 0.02<!-- /slot -->" in deck
    assert "(<!-- slot:ab_where -->MI300X, 2026-10-12<!-- /slot -->): <!-- slot:ab_str_wrong -->7 of 20 raw STRs, " \
           "0 of 20 Wapsi-bound STRs<!-- /slot -->" in deck  # like with like
    # a droplet run never fills what the pitch words as a Token Factory figure, nor a hand-filled slot
    for sid in ("tf_demo_pass", "tf_demo_fallback", "tf_call_latency", "ab_raw_wrong", HAND_FILLED):
        assert f"TBD({sid})" in deck and f"<!-- slot:{sid} -->" not in deck, sid
    assert "filled by hand from their own results files" in out and HAND_FILLED in out
    for sid in EXTRA_IDS:  # every optional slot of the page filled
        assert f"TBD({sid})" not in deck, sid

    check = _checker()
    rows = check.parse_rows(pitch / "NUMBERS.md")
    amd_rows = [r for r in rows if r.rid.startswith("amd_")]
    assert len(amd_rows) > 20
    errors, resolved = check.check_numbers(amd_rows)  # each row re-reads the evidence bundle
    assert errors == []
    assert resolved["amd_llm_tok_s_c16"] == 600.0
    registered = {check._norm(t) for r in rows for t in check.TOKEN_RE.findall(r.pitched)}
    for page in (pitch / "amd" / "DECK_OUTLINE.md", pitch / "amd" / "VIDEO_SCRIPT.md"):
        for tok in check.TOKEN_RE.findall(page.read_text(encoding="utf-8")):
            assert check._norm(tok) in registered, (page.name, tok)
    # the open-slots table: a slot filled everywhere loses its row, an open one keeps it
    still_listed = check.parse_slots(pitch / "NUMBERS.md")
    assert not {"droplet_vram", "droplet_tokens_s", "gnn_amd_auc", "gpu_lora", "ab_where"} & set(still_listed)
    assert {"tf_demo_pass", "ab_raw_wrong", "tf_call_latency", HAND_FILLED} <= set(still_listed)

    # same evidence again: nothing changes; newer evidence: values replaced in place, no duplicate section
    assert fill_pitch.main([*args, "--write"]) == 0
    assert (pitch / "amd" / "DECK_OUTLINE.md").read_text(encoding="utf-8") == deck
    ev = json.loads(bundle.read_text(encoding="utf-8"))
    ev["headline"]["llm_tok_s_c1"] = 55.0
    bundle.write_text(json.dumps(ev), encoding="utf-8")
    assert fill_pitch.main([*args, "--write"]) == 0
    deck2 = (pitch / "amd" / "DECK_OUTLINE.md").read_text(encoding="utf-8")
    assert "-->55 / 180 / 600 tokens/s" in deck2 and "-->50 / 180" not in deck2
    numbers = (pitch / "NUMBERS.md").read_text(encoding="utf-8")
    assert numbers.count(fill_pitch.END) == 1 and "| amd_llm_tok_s_c1 | 55 " in numbers


def test_fill_pitch_leaves_unmeasured_slots_tbd(tmp_path, capsys):
    pitch, bundle = _pitch_copy(tmp_path), _fixture_bundle(tmp_path)
    ev = json.loads(bundle.read_text(encoding="utf-8"))
    for k in [k for k in ev["headline"] if k.startswith(("lora_", "e2e_best_inr"))]:
        ev["headline"][k] = None
    bundle.write_text(json.dumps(ev), encoding="utf-8")
    assert fill_pitch.main(["--evidence", str(bundle), "--pitch-dir", str(pitch), "--allow-fixture", "--write"]) == 0
    deck = (pitch / "amd" / "DECK_OUTLINE.md").read_text(encoding="utf-8")
    assert "LoRA: TBD(gpu_lora)." in deck  # not measured: still an open slot, never a guess
    assert "<!-- slot:gpu_cost_case -->$0.0022<!-- /slot -->" in deck  # no rupee figure without the day's rate
    assert "gpu_lora" in capsys.readouterr().out
    assert "gpu_lora" in _checker().parse_slots(pitch / "NUMBERS.md")  # and still listed as open


def test_fill_pitch_refuses_rehearsal_invalid_and_fixture_on_real_pitch(tmp_path, capsys):
    real = {p: p.read_bytes() for p in (ROOT / "pitch").rglob("*.md")}
    evid = make_evidence(tmp_path / "r")
    reh = collect_evidence.build(evid, "rehearsal")
    (tmp_path / "reh.json").write_text(json.dumps(reh), encoding="utf-8")
    assert fill_pitch.main(["--evidence", str(tmp_path / "reh.json"), "--write"]) == fill_pitch.EXIT_REFUSED
    early = collect_evidence.build(make_evidence(tmp_path / "e", started="2026-10-10T00:00:00Z"), "gpu")
    (tmp_path / "early.json").write_text(json.dumps(early), encoding="utf-8")
    assert fill_pitch.main(["--evidence", str(tmp_path / "early.json"), "--write"]) == fill_pitch.EXIT_REFUSED
    fixture = _fixture_bundle(tmp_path)
    assert fill_pitch.main(["--evidence", str(fixture), "--write"]) == fill_pitch.EXIT_REFUSED
    assert fill_pitch.main(["--evidence", str(fixture), "--write", "--allow-fixture"]) == fill_pitch.EXIT_REFUSED
    out = capsys.readouterr().out
    assert out.count("REFUSED") == 4 and "rehearsal numbers never go into the pitch" in out
    assert {p: p.read_bytes() for p in (ROOT / "pitch").rglob("*.md")} == real


def test_inr_and_rate_formats():
    assert fill_pitch.INR.text(0.1866) == "₹0.19"
    assert fill_pitch.INR.text(123456.499) == "₹1,23,456.50"
    assert fill_pitch.RATE.text(1.0) == "1.0" and fill_pitch.RATE.text(0.95) == "0.95"
    assert fill_pitch.USD.text(0.00221) == "$0.0022" and fill_pitch.USD.text(12.5) == "$12.50"
    # below $1 two significant figures, never $0.0000; the Value column uses the same decimals
    assert (fill_pitch.USD.text(0.0000123), fill_pitch.USD.value(0.0000123)) == ("$0.000012", "0.000012")
    assert (fill_pitch.USD.text(0.000999), fill_pitch.USD.value(0.000999)) == ("$0.0010", "0.0010")
    assert (fill_pitch.USD.text(0.5), fill_pitch.USD.value(0.5)) == ("$0.50", "0.50")
    assert (fill_pitch.USD.text(0.0), fill_pitch.USD.value(12.5)) == ("$0.00", "12.50")
    assert (fill_pitch.INR.text(0.00104), fill_pitch.INR.value(0.00104)) == ("\u20b90.0010", "0.0010")
    assert fill_pitch.INR.value(0.1866) == "0.19"


# --------------------------------------------------------------------------- nothing claims GPU numbers


def test_committed_evidence_never_predates_the_event():
    try:
        tracked = subprocess.run(["git", "ls-files", "editions/amd/evidence"], cwd=ROOT, capture_output=True,
                                 text=True, timeout=30, check=True).stdout.split()
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git not available")
    assert not [f for f in tracked if "/cpu-rehearsal/" in f]  # rehearsal bundles are never committed
    for f in tracked:
        if f.endswith("/evidence.json"):
            ev = json.loads((ROOT / f).read_text(encoding="utf-8"))
            assert ev["mode"] in ("droplet", "notebook", "endpoint", "gpu"), f
            assert ev.get("results_valid", ev.get("gpu_results_valid")) is True, f
            assert ev["gpu_results_valid"] is (ev["mode"] != "endpoint"), f  # an endpoint run has no GPU of its own
            assert ev["date"] >= "2026-10-12", f


# --------------------------------------------------------------------------- A/B auditor, mock streaming, bench names


def test_ab_auditor_counts_raw_errors_and_passes_case_values():
    from editions.amd.kickoff import ab_hallucination as ab
    from wapsi.fixtures.bundles import showcase_bundle

    bundle = showcase_bundle(1)
    msgs = ab.raw_messages(bundle)
    assert "<<<UNTRUSTED_COMPLAINT_TEXT>>>" in msgs[1]["content"]
    assert bundle.complaints[0].victim_name not in msgs[1]["content"]  # no names or phone numbers to the model
    assert bundle.complaints[0].victim_mobile not in msgs[1]["content"]
    good = ab.RawStubLLM(hallucinate=False).complete(msgs)
    assert ab._clean(ab.audit_text(good, bundle))
    bad = ab.RawStubLLM(hallucinate=True).complete(msgs)
    a = ab.audit_text(bad, bundle)
    assert len(a["wrong_amounts"]) == 1 and a["invented_accounts"] == ["ZZ99-0000000007"]
    assert "[RBI-FRM-MD-2024]" in a["unknown_citations"] and any("CrPC" in c for c in a["unknown_citations"])
    rows = [{"case_id": "x", "docs": 1, "audits": [a], "clean": [False]},
            {"case_id": "y", "docs": 1, "audits": [ab.audit_text(good, bundle)], "clean": [True]}]
    s = ab.summarize(rows, "raw")
    assert (s["docs"], s["docs_with_wrong_amount"], s["docs_clean"], s["clean_rate"]) == (2, 1, 1, 0.5)


def test_mock_endpoint_streams_like_vllm_and_llm_bench_reads_it():
    from editions.amd.bench import llm_bench
    from editions.amd.lora.mock_endpoint import stream_events
    from wapsi.adapters.llm_fake import FakeLLM

    llm = FakeLLM(name="stub")
    body = {"messages": [{"role": "user", "content": "hello there"}], "stream": True,
            "stream_options": {"include_usage": True}}
    events = stream_events(llm, body, "rehearsal-fake-llm")
    text = "".join((e["choices"][0]["delta"].get("content") or "") for e in events if e["choices"])
    assert text == "No structured drafting request was found."
    assert events[-1]["usage"]["completion_tokens"] == len(text.split()) and events[0]["model"] == "rehearsal-fake-llm"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "rehearsal-fake-llm"}]})
        sse = "".join(f"data: {json.dumps(e)}\n\n" for e in stream_events(llm, json.loads(request.content)))
        return httpx.Response(200, text=sse + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    cfg = llm_bench.BenchConfig(base_url="http://mock.test/v1", levels=(1, 2), requests=2, output_len=4, warmup=0)
    res = asyncio.run(llm_bench.run_bench(cfg, transport=httpx.MockTransport(handler)))
    assert res["model"] == "rehearsal-fake-llm"
    assert all(lv["completed"] == 2 and lv["tokens_source"] == "usage" for lv in res["levels"])


def test_e2e_bench_honours_name(tmp_path):
    from editions.amd.bench import e2e_bench

    assert e2e_bench.main(["--world", "showcase-memory", "--fake-llm", "--no-search", "--limit", "1",
                           "--name", "e2e_c3", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "e2e_c3.json").exists() and (tmp_path / "e2e_c3.md").exists()
    assert not (tmp_path / "report.json").exists()  # a concurrency sweep no longer overwrites one file


# --------------------------------------------------------------------------- review fixes: measurement failures, identity


def _source(bash: str, snippet: str, *flags: str) -> subprocess.CompletedProcess:
    """Source run_all.sh (it stops after defining its helpers and stages; nothing runs), then run ``snippet``."""
    script = (KICK / "run_all.sh").as_posix()
    return _bash(bash, "-c", f'source "{script}" {" ".join(flags)}; {snippet}', env=_bash_env())


def test_measure_records_failures_in_gpu_mode_and_stays_strict_in_the_rehearsal(bash, tmp_path):
    notes = (tmp_path / "bench.notes").as_posix()
    gpu = _source(bash, f'NOTES="{notes}"; measure llm_bench false; measure llm_bench_verifier true; '
                        f'measure e2e_c16 sh -c "exit 3"; echo "stage goes on: $?"', "--mode", "droplet", "--date",
                  "2026-10-12")
    assert gpu.returncode == 0, gpu.stdout + gpu.stderr
    assert "stage goes on: 0" in gpu.stdout and "measurement llm_bench exited 1" in gpu.stdout
    assert stagelog.read_notes(Path(notes)) == {"measure_failures": "2", "measure_failed": "llm_bench,e2e_c16"}
    rehearsal = _source(bash, f'NOTES="{notes}"; measure llm_bench false; echo unreachable', "--rehearsal")
    assert rehearsal.returncode != 0 and "unreachable" not in rehearsal.stdout  # the rehearsal stays strict


def test_bench_and_ab_measure_every_request_and_free_the_eval_port(bash):
    plan = _run(bash, "--mode", "droplet", "--dry-run", "--force", "--date", "2026-10-12", "--from", "lora-eval").stdout
    serve = plan.split("] serve:")[1].split("== [")[0]
    assert [line for line in serve.splitlines() if line.startswith("  $")][0].endswith(
        "docker rm -f wapsi-eval-base wapsi-eval-tuned")  # a failed lora-eval may still hold port 8000
    assert "amd_smi_metric_raw.json' || true" in plan
    text = (KICK / "run_all.sh").read_text(encoding="utf-8")
    for name in ("llm_bench_base", "llm_bench", "llm_bench_wapsi_prompts", "llm_bench_verifier", '"e2e_c${c}"',
                 "ab_hallucination", "golden_eval"):
        assert f"measure {name} py -m" in text, name
    cleanup = _source(bash, "DRY_RUN=1; gpu_lora_eval_on_failure", "--mode", "droplet", "--date", "2026-10-12")
    assert "docker rm -f wapsi-eval-base wapsi-eval-tuned" in cleanup.stdout
    assert '"${fn}_on_failure"' in text  # the runner calls a stage's cleanup after it fails


def test_later_week_run_captures_gpu_identity_and_commit(bash):
    out = _run(bash, "--mode", "droplet", "--dry-run", "--date", "2026-10-14", "--stage", "bench,ab,evidence").stdout
    evidence = out.split("] evidence:")[1]
    assert "stagelog.py static editions/amd/evidence/2026-10-14/amd_smi_static.json" in evidence
    assert "# note git_commit=" in evidence and "# note amd_baseline=" in evidence
    assert "then: uv run python editions/amd/kickoff/fill_pitch.py" in evidence and ": py " not in evidence


def test_later_week_bundle_without_static_keeps_the_gpu_identity(tmp_path):
    evid = make_evidence(tmp_path, date="2026-10-14", started="2026-10-14T09:00:00Z", static=False)
    ev = collect_evidence.build(evid, "gpu")
    assert ev["gpu_results_valid"] is True and ev["headline"]["gpu_name"] == "AMD Instinct MI300X"
    assert ev["hardware"]["gpu_name_source"] == "bench/telemetry_summary.json (amd-smi)"
    assert "GPU name from bench/telemetry_summary.json" in collect_evidence.to_markdown(ev)
    (evid / "bench" / "telemetry_summary.json").unlink()
    nothing = collect_evidence.build(evid, "gpu")
    assert nothing["gpu_results_valid"] is False and any("MI300X" in r for r in nothing["invalid_reasons"])


def _edit_json(path: Path, fn) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_measurement_errors_are_warnings_never_headline_numbers(tmp_path):
    evid = make_evidence(tmp_path)
    _edit_json(evid / "bench" / "llm_bench.json",
               lambda d: next(lv for lv in d["levels"] if lv["concurrency"] == 16).update(completed=6, errors=2))
    _edit_json(evid / "bench" / "e2e_c16.json", lambda d: d["metrics"].update(cases_ok=36, cases_failed=3))
    _edit_json(evid / "stages" / "02-bench.json",
               lambda d: d.update(notes={"measure_failures": "2", "measure_failed": "llm_bench,e2e_c16"},
                                  log="logs/09-bench.log"))
    ev = collect_evidence.build(evid, "gpu")
    h = ev["headline"]
    assert h["llm_tok_s_c16"] is None and h["llm_ttft_p95_s_c16"] is None and h["llm_tok_s_c4"] == 180.0
    assert h["llm_peak_tok_s"] == 1500.4  # the best level without errors
    assert h["e2e_cases_per_hour_c16"] is None and h["e2e_best_concurrency"] == 8
    text = " | ".join(ev["warnings"])
    assert "concurrency 16: 2 of 8 requests failed" in text and "3 of 39 cases failed" in text
    assert "stage bench: 2 measurement(s) exited non-zero (llm_bench,e2e_c16)" in text
    assert "## Warnings" in collect_evidence.to_markdown(ev)
    _edit_json(evid / "ab_hallucination.json", lambda d: (d.update(cases_compared=0),
                                                          d["arms"]["raw"].update(case_errors=20)))
    ev = collect_evidence.build(evid, "gpu")
    assert ev["headline"]["ab_raw_docs"] is None and ev["headline"]["ab_bound_str_docs"] is None
    assert any("no case was completed by both arms" in w for w in ev["warnings"])


# --------------------------------------------------------------------------- fill_pitch: several bundles


def _bundle(tmp_path: Path, name: str, *, date: str, started: str, drop: tuple[str, ...] = (),
            set_values: dict | None = None) -> Path:
    evid = make_evidence(tmp_path / name, date=date, started=started)
    ev = collect_evidence.build(evid, "gpu")
    ev["synthetic_fixture"] = True
    for k in [k for k in ev["headline"] if drop and k.startswith(drop)]:
        ev["headline"][k] = None
    ev["headline"].update(set_values or {})
    path = evid / "evidence.json"
    path.write_text(json.dumps(ev), encoding="utf-8")
    return path


def test_fill_pitch_merges_a_later_bundle_instead_of_dropping_earlier_rows(tmp_path):
    pitch = _pitch_copy(tmp_path)
    common = ["--pitch-dir", str(pitch), "--allow-fixture", "--write", "--strict"]
    first = _bundle(tmp_path, "a", date="2026-10-12", started="2026-10-12T15:30:00Z")
    assert fill_pitch.main(["--evidence", str(first), *common]) == 0
    # Wednesday: serving and e2e re-measured (new values); no GNN, LoRA, A/B or device-peak VRAM in this bundle
    second = _bundle(tmp_path, "b", date="2026-10-14", started="2026-10-14T09:00:00Z",
                     drop=("gnn_", "lora_", "ab_", "vram_peak"), set_values={"llm_tok_s_c1": 77.0, "vram_llm_gb": 99.9})
    assert fill_pitch.main(["--evidence", str(second), *common]) == 0

    deck = (pitch / "amd" / "DECK_OUTLINE.md").read_text(encoding="utf-8")
    video = (pitch / "amd" / "VIDEO_SCRIPT.md").read_text(encoding="utf-8")
    assert "-->77 / 180 / 600 tokens/s at concurrency 1 / 4 / 16<!-- /slot -->" in deck  # refilled from the later bundle
    assert "7 of 20 raw STRs" in deck and "0.985 (MI300X)" in deck  # kept from the first bundle
    assert "Measured on <!-- slot:gpu_where -->MI300X, 2026-10-12 to 2026-10-14<!-- /slot -->." in deck
    assert "(<!-- slot:ab_where -->MI300X, 2026-10-12<!-- /slot -->)" in deck  # the A/B is still the first bundle's
    assert "<!-- slot:droplet_vram -->99.9 GB / 28.3 GB / 2.0 GB" in deck  # the deck's own VRAM slot refilled
    assert "drafting model 102.5 GB, verifier 28.3 GB, GNN 2.0 GB; device peak 136.7 GB" in deck  # device peak kept

    check = _checker()
    rows = [r for r in check.parse_rows(pitch / "NUMBERS.md") if r.rid.startswith("amd_")]
    errors, resolved = check.check_numbers(rows)
    assert errors == []  # every row, old and new, re-reads its own bundle
    by_id = {r.rid: r for r in rows}
    assert by_id["amd_llm_tok_s_c1"].source.endswith("2026-10-14/evidence.json") and resolved["amd_llm_tok_s_c1"] == 77
    assert by_id["amd_ab_raw_docs"].source.endswith("2026-10-12/evidence.json")
    assert by_id["amd_lora_tuned_fallback_rate"].source.endswith("2026-10-12/evidence.json")
    assert (by_id["amd_vram_llm_gb"].value, by_id["amd_vram_llm_gb_20261012"].value) == ("99.9", "102.5")
    registered = {check._norm(t) for r in check.parse_rows(pitch / "NUMBERS.md") for t in check.TOKEN_RE.findall(r.pitched)}
    for page in (deck, video):
        for tok in check.TOKEN_RE.findall(page):
            assert check._norm(tok) in registered, tok
    numbers = (pitch / "NUMBERS.md").read_text(encoding="utf-8")
    assert numbers.count(fill_pitch.END) == 1 and "MI300X runs on 2026-10-12, 2026-10-14 (UTC)" in numbers

    # the first bundle again (e.g. a re-run of fill_pitch): its values come back, nothing is lost or duplicated
    assert fill_pitch.main(["--evidence", str(first), *common]) == 0
    rows = [r for r in check.parse_rows(pitch / "NUMBERS.md") if r.rid.startswith("amd_")]
    assert check.check_numbers(rows)[0] == [] and len({r.rid for r in rows}) == len(rows)
    assert "-->50 / 180 / 600 tokens/s" in (pitch / "amd" / "DECK_OUTLINE.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- A/B: API key, like with like


def test_ab_sends_the_vllm_api_key_and_records_a_401_as_a_case_error(monkeypatch, tmp_path):
    from editions.amd.kickoff import ab_hallucination as ab

    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != f"Bearer {API_SECRET}":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Grounds of suspicion: layered "
                                                                              "transfers, see the case record."}}]})

    args = ["--llm-url", "http://vllm.test/v1", "--world", "showcase-memory", "--limit", "1", "--workers", "1",
            "--out", str(tmp_path / "ab.json")]
    monkeypatch.delenv("WAPSI_LLM_API_KEY", raising=False)
    monkeypatch.setenv("VLLM_API_KEY", API_SECRET)  # the RUNBOOK's variable
    assert ab.main(args, transport=httpx.MockTransport(handler)) == 0
    rep_text = (tmp_path / "ab.json").read_text(encoding="utf-8")
    rep = json.loads(rep_text)
    assert seen and set(seen) == {f"Bearer {API_SECRET}"}
    assert rep["errors"] == [] and rep["cases_compared"] == 1 and rep["arms"]["bound"]["str_only"]["docs"] == 1
    assert API_SECRET not in rep_text and API_SECRET not in (tmp_path / "ab.md").read_text(encoding="utf-8")

    monkeypatch.delenv("VLLM_API_KEY")  # no key: the raw arm's 401 is a recorded case error, nothing is compared
    assert ab.main(args, transport=httpx.MockTransport(handler)) == 1
    rep = json.loads((tmp_path / "ab.json").read_text(encoding="utf-8"))
    assert rep["arms"]["raw"]["case_errors"] == 1 and rep["cases_compared"] == 0
    assert "401" in rep["errors"][0]["error"]


def test_ab_compares_the_same_cases_and_strs_alone():
    from editions.amd.kickoff import ab_hallucination as ab

    clean = {"wrong_amounts": [], "invented_accounts": [], "invented_utrs": [], "unknown_citations": []}
    bad = {**clean, "wrong_amounts": ["₹9,737"]}
    raw = [{"case_id": "x", "docs": 1, "audits": [bad], "clean": [False]}, {"case_id": "y", "error": "LLMError: 500"}]
    bound = [{"case_id": "x", "docs": 2, "audits": [clean, bad], "clean": [True, False], "verifier_passed": [True, True],
              "kinds": ["STR", "HOLD_INSTRUCTION"], "llm_drafts": 1, "prose_fallbacks": 0},
             {"case_id": "y", "docs": 1, "audits": [bad], "clean": [False], "verifier_passed": [False],
              "kinds": ["STR"], "llm_drafts": 1, "prose_fallbacks": 1}]
    paired = ab.compared_cases(raw, bound)
    assert paired == {"x"}  # y failed in the raw arm: left out of both arms
    r, b = ab.summarize(raw, "raw", paired), ab.summarize(bound, "bound", paired)
    assert (r["docs"], r["case_errors"], r["docs_with_wrong_amount"]) == (1, 1, 1)
    assert (b["docs"], b["cases_compared"], b["docs_with_wrong_amount"]) == (2, 1, 1)
    assert (b["str_only"]["docs"], b["str_only"]["docs_with_wrong_amount"], b["str_only"]["verifier_pass_rate"]) == (
        1, 0, 1.0)
    md = ab.to_markdown({"world": "w", "cases": 2, "cases_compared": 1, "label": "fixture", "llm": "x",
                         "generated_at": "t", "arms": {"raw": r, "bound": b}})
    assert "Like with like, one STR per case in each arm" in md and "| STRs | 1 | 1 |" in md


def test_e2e_bench_sends_the_vllm_api_key_to_both_models(monkeypatch, tmp_path):
    from editions.amd.bench import e2e_bench
    from wapsi.adapters import llm_openai_compat
    from wapsi.adapters.llm_fake import FakeLLM

    seen: list[tuple[str, str | None]] = []

    class Capture(FakeLLM):
        def __init__(self, base_url: str, model: str, api_key: str | None = None, **kw) -> None:
            seen.append((base_url, api_key))
            super().__init__(name=kw.get("name") or model)

    monkeypatch.setattr(llm_openai_compat, "OpenAICompatLLM", Capture)
    monkeypatch.setattr(e2e_bench, "model_card", lambda url, key: {})
    for k in ("WAPSI_LLM_API_KEY", "WAPSI_VERIFIER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VLLM_API_KEY", API_SECRET)
    e2e_bench.main(["--world", "showcase-memory", "--no-search", "--limit", "1", "--llm-url", "http://vllm.test/v1",
                    "--verifier-url", "http://verifier.test/v1", "--name", "e2e_c1", "--out", str(tmp_path)])
    assert seen == [("http://vllm.test/v1", API_SECRET), ("http://verifier.test/v1", API_SECRET)]
    assert API_SECRET not in (tmp_path / "e2e_c1.json").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- training image vs the ROCm torch


def test_training_libraries_accept_the_rocm_image_torch():
    import re

    from editions.amd.kickoff import train_image_check as tic

    pins = {f: set(re.findall(r"pytorch_release_(\d+\.\d+\.\d+)", (ROOT / f).read_text(encoding="utf-8")))
            for f in ("editions/amd/kickoff/Dockerfile.train", "editions/amd/kickoff/run_all.sh",
                      "editions/amd/gnn/Dockerfile")}
    versions = set().union(*pins.values())
    assert len(versions) == 1, pins  # one ROCm torch for the training image, the run and the GNN image
    for dist in ("transformers", "peft", "accelerate"):
        pytest.importorskip(dist)
    rows = tic.check_torch_ranges(versions.pop())  # the uv.lock pins, as installed in this venv
    assert all(r["ok"] for r in rows.values()), rows
    assert rows["transformers"]["declares"]  # transformers declares a torch floor: the check is not vacuous
    assert tic.check_torch_ranges("2.4.0")["transformers"]["ok"] is False


def test_train_image_check_runs_one_lora_step_on_cpu(tmp_path):
    for mod in ("torch", "transformers", "peft", "trl", "datasets"):
        pytest.importorskip(mod)
    import torch

    from editions.amd.kickoff import train_image_check as tic

    assert tic.main(["--device", "cpu", "--out", str(tmp_path / "check.json")]) == 0
    rep = json.loads((tmp_path / "check.json").read_text(encoding="utf-8"))
    assert rep["ok"] and rep["imports"]["trl"]["ok"] and rep["lora_step"]["tensors_changed"] > 0
    assert rep["lora_step"]["loss_finite"] and rep["torch_ranges"]["transformers"]["ok"]
    if not torch.cuda.is_available():  # the droplet passes --require-gpu: a container without the GPU fails here
        assert tic.main(["--device", "cpu", "--require-gpu"]) == 1


# --------------------------------------------------------------------------- CPU rehearsal smoke (marked)


@pytest.mark.rehearsal
def test_cpu_rehearsal_smoke(bash, tmp_path):
    """run_all.sh --rehearsal end to end on the fast stages: records, markers, labels, the fill_pitch refusal."""
    evid_root, work = (tmp_path / "evidence").as_posix(), (tmp_path / "work").as_posix()
    full = os.environ.get("WAPSI_FULL_REHEARSAL") == "1"  # opt in: every stage (GNN + LoRA on CPU, minutes)
    stages = [] if full else ["--stage", "models,eval,ab,evidence"]
    res = _run(bash, "--rehearsal", "--skip-preflight", "--date", "2026-10-07", "--evidence-root", evid_root,
               "--work", work, *stages, timeout=3600 if full else 900)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
    evid = tmp_path / "evidence" / "cpu-rehearsal" / "2026-10-07"
    recs = {json.loads(p.read_text(encoding="utf-8"))["stage"]: json.loads(p.read_text(encoding="utf-8"))
            for p in (evid / "stages").glob("*.json")}
    assert {"models", "eval", "ab", "evidence"} <= set(recs)
    for rec in recs.values():
        assert rec["status"] == "ok" and rec["mode"] == "rehearsal" and "not a GPU result" in rec["label"]
        assert rec["budget"]["billed"] is False
    assert (evid / "stages" / "11-ab.ok").exists() and (evid / "logs" / "11-ab.log").exists()
    snap = json.loads((evid / "amd_smi" / "11-ab-before.json").read_text(encoding="utf-8"))
    if not shutil.which("amd-smi"):
        assert snap["available"] is False
    ev = json.loads((evid / "evidence.json").read_text(encoding="utf-8"))
    assert ev["mode"] == "cpu-rehearsal" and ev["gpu_results_valid"] is False and "CPU REHEARSAL" in ev["label"]
    assert ev["headline"]["ab_bound_docs_with_wrong_amount"] == 0 and ev["headline"]["ab_raw_docs_with_wrong_amount"] > 0
    assert ev["headline"]["golden_cases"] == 2 and ev["results"]["golden"]["stub"] is True  # the eval stage ran
    assert ev["results_valid"] is False and ev["compute"]["kind"] == "cpu-rehearsal"
    assert "ok: fill_pitch.py refused the rehearsal evidence" in res.stdout
    assert (evid / "evidence.md").read_text(encoding="utf-8").startswith("# Wapsi AMD evidence: CPU rehearsal")
    assert not (tmp_path / "evidence" / "cpu-rehearsal" / ".active").exists()  # finished run clears the pointer
    # resumable: the plan of a re-run skips every stage that has a marker
    again = _run(bash, "--rehearsal", "--dry-run", "--date", "2026-10-07", "--evidence-root", evid_root,
                 "--work", work)
    assert again.returncode == 0
    for name in ("models", "eval", "ab", "evidence"):
        assert f"{name}: done earlier" in again.stdout
