"""AMD benchmarks (offline): stats helpers, streaming LLM bench vs a fake OpenAI server, GPU telemetry parsers on
recorded-shape fixtures, the end-to-end bench with FakeLLM, and the merged results report."""

from __future__ import annotations

import asyncio
import json
import shutil

import httpx
import pytest

from editions.amd.bench import common, e2e_bench, gpu_telemetry, llm_bench, report

# --------------------------------------------------------------------------- stats helpers


def test_percentile_and_dist():
    assert common.percentile([], 50) is None
    assert common.percentile([3.0], 95) == 3.0
    assert common.percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert common.percentile(range(1, 101), 95) == pytest.approx(95.05)
    d = common.dist([0.1, 0.2, 0.3, None, float("nan")])
    assert d["n"] == 3 and d["p50"] == pytest.approx(0.2) and d["mean"] == pytest.approx(0.2) and d["max"] == 0.3


def test_summarize_level_math():
    R = llm_bench.RequestRecord
    recs = [
        R(ok=True, start=0.0, end=1.1, ttft=0.1, chunk_times=[0.1, 0.2, 0.3], output_tokens=11, prompt_tokens=100),
        R(ok=True, start=0.0, end=2.3, ttft=0.3, chunk_times=[0.3, 0.7], output_tokens=21, prompt_tokens=100),
        R(ok=False, start=0.0, end=0.5, error="HTTP 500: boom"),
    ]
    s = llm_bench.summarize_level(recs, concurrency=2, duration_s=2.0)
    assert (s["requests"], s["completed"], s["errors"]) == (3, 2, 1)
    assert s["error_samples"] == ["HTTP 500: boom"]
    assert s["ttft_s"]["p50"] == pytest.approx(0.2)
    assert s["itl_s"]["n"] == 3 and s["itl_s"]["max"] == pytest.approx(0.4)
    assert s["tpot_s"]["p50"] == pytest.approx(0.1)  # (1.1-0.1)/10 and (2.3-0.3)/20
    assert s["output_tokens"] == 32 and s["input_tokens"] == 200 and s["tokens_source"] == "usage"
    assert s["output_tokens_per_s"] == pytest.approx(16.0)
    assert s["requests_per_s"] == pytest.approx(1.0)
    assert s["output_tokens_per_s_per_user"] == pytest.approx(10.0)


# --------------------------------------------------------------------------- fake streaming OpenAI server


def _fake_openai(ttft=0.1, itl=0.02, tokens=10, status=200, usage=True, seen=None):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "wapsi-llm", "root": "/models/m", "max_model_len": 4096}]})
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "overloaded"}})
        n = min(tokens, body.get("max_tokens", tokens))

        async def stream():
            await asyncio.sleep(ttft)
            yield b'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
            for i in range(n):
                if i:
                    await asyncio.sleep(itl)
                chunk = {"choices": [{"index": 0, "delta": {"content": f"t{i} "}}]}
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            if usage:
                u = {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": n, "total_tokens": 50 + n}}
                yield f"data: {json.dumps(u)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())

    return httpx.MockTransport(handler)


def test_llm_bench_measures_ttft_throughput_and_concurrency():
    seen: list[dict] = []
    cfg = llm_bench.BenchConfig(base_url="http://llm.test/v1", levels=(1, 4), requests=8, input_len=64,
                                output_len=10, warmup=0)
    res = asyncio.run(llm_bench.run_bench(cfg, transport=_fake_openai(seen=seen)))
    assert res["kind"] == "llm_bench" and res["model"] == "wapsi-llm" and res["model_root"] == "/models/m"
    one, four = res["levels"]
    for lv in (one, four):
        assert (lv["completed"], lv["errors"]) == (8, 0)
        assert lv["output_tokens"] == 80 and lv["input_tokens"] == 400 and lv["tokens_source"] == "usage"
        assert 0.08 <= lv["ttft_s"]["p50"] <= 0.6
        assert lv["itl_s"]["n"] == 8 * 9
        assert lv["output_tokens_per_s"] == pytest.approx(80 / lv["duration_s"], rel=0.01)
    # four in flight finish the same work much faster than one at a time
    assert four["requests_per_s"] > 2.0 * one["requests_per_s"]
    assert four["duration_s"] < one["duration_s"] / 2
    body = seen[0]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["ignore_eos"] is True and body["min_tokens"] == 10 and body["max_tokens"] == 10
    assert len({b["messages"][0]["content"] for b in seen}) == len(seen)  # unique nonces defeat prefix caching
    md = llm_bench.to_markdown(res)
    assert "| Concurrency |" in md and "wapsi-llm" in md


def test_llm_bench_counts_chunks_without_usage_and_reports_errors():
    cfg = llm_bench.BenchConfig(base_url="http://llm.test/v1", levels=(2,), requests=4, output_len=5, warmup=0)
    res = asyncio.run(llm_bench.run_bench(cfg, transport=_fake_openai(ttft=0.0, itl=0.0, tokens=5, usage=False)))
    lv = res["levels"][0]
    assert lv["tokens_source"] == "chunks" and lv["output_tokens"] == 20
    bad = asyncio.run(llm_bench.run_bench(cfg, transport=_fake_openai(status=503)))
    lv = bad["levels"][0]
    assert lv["completed"] == 0 and lv["errors"] == 4 and "HTTP 503" in lv["error_samples"][0]


def test_wapsi_prompts_replay_the_pipeline_requests():
    prompts = llm_bench.wapsi_prompts()
    assert len(prompts) >= 3
    assert all(p.messages and not p.fixed_length for p in prompts)
    payload = llm_bench._payload(llm_bench.BenchConfig(base_url="x"), "m", prompts[0])
    assert "ignore_eos" not in payload
    assert any('"task"' in m["content"] for m in prompts[0].messages)


# --------------------------------------------------------------------------- GPU telemetry parsers


def _fixture_json(bench_fixtures, name):
    return json.loads((bench_fixtures / name).read_text(encoding="utf-8"))


def test_telemetry_parses_amd_smi_metric_fixture(bench_fixtures):
    (g,) = gpu_telemetry.parse_amd_smi_metric(_fixture_json(bench_fixtures, "amd_smi_metric.json"))
    assert g.gpu == 0
    assert (g.vram_used_mb, g.vram_total_mb) == (142000.0, 196592.0)
    assert (g.gfx_activity_pct, g.umc_activity_pct, g.power_w) == (87.0, 41.0, 512.0)
    assert (g.temp_hotspot_c, g.temp_mem_c) == (61.0, 48.0)


def test_telemetry_parses_legacy_upper_case_strings(bench_fixtures):
    (g,) = gpu_telemetry.parse_amd_smi_metric(_fixture_json(bench_fixtures, "amd_smi_metric_legacy.json"))
    assert (g.vram_used_mb, g.vram_total_mb, g.gfx_activity_pct, g.power_w) == (283.0, 196592.0, 12.0, 140.0)
    assert g.temp_hotspot_c == 40.0


def test_telemetry_parses_processes_and_static(bench_fixtures):
    procs = gpu_telemetry.parse_amd_smi_process(_fixture_json(bench_fixtures, "amd_smi_process.json"))
    assert [p.pid for p in procs] == [4101, 4202, 4303]
    assert procs[0].vram_mb == pytest.approx(102400.0) and procs[1].vram_mb == pytest.approx(27648.0)
    assert procs[2].vram_mb == pytest.approx(4096.0) and procs[2].name == "uvicorn"
    assert gpu_telemetry.parse_amd_smi_process(_fixture_json(bench_fixtures, "amd_smi_process_empty.json")) == []
    assert gpu_telemetry.parse_amd_smi_static(_fixture_json(bench_fixtures, "amd_smi_static.json")) == {
        0: "AMD Instinct MI300X"}


def test_telemetry_parses_rocm_smi_fallback(bench_fixtures):
    (g,) = gpu_telemetry.parse_rocm_smi(_fixture_json(bench_fixtures, "rocm_smi.json"))
    assert g.gpu == 0 and g.gfx_activity_pct == 35.0 and g.power_w == 420.0
    assert g.vram_total_mb == pytest.approx(206141652992 / 2**20, rel=1e-6)
    assert g.vram_used_mb == pytest.approx(123684991795 / 2**20, rel=1e-6)


def test_telemetry_maps_processes_to_containers(bench_fixtures):
    names = gpu_telemetry.parse_docker_ps((bench_fixtures / "docker_ps.txt").read_text(encoding="utf-8"))
    assert names["1" * 64] == "wapsi-amd-llm-1"
    v2 = gpu_telemetry.container_id_from_cgroup((bench_fixtures / "proc_4101_cgroup_v2.txt").read_text())
    v1 = gpu_telemetry.container_id_from_cgroup((bench_fixtures / "proc_4202_cgroup_v1.txt").read_text())
    assert (v2, v1) == ("1" * 64, "2" * 64)
    assert gpu_telemetry.container_id_from_cgroup((bench_fixtures / "proc_4303_cgroup_host.txt").read_text()) is None


def _fake_proc(tmp_path, bench_fixtures):
    root = tmp_path / "proc"
    for pid, name in ((4101, "proc_4101_cgroup_v2.txt"), (4202, "proc_4202_cgroup_v1.txt"),
                      (4303, "proc_4303_cgroup_host.txt")):
        (root / str(pid)).mkdir(parents=True)
        shutil.copy(bench_fixtures / name, root / str(pid) / "cgroup")
    return root


def _runner(bench_fixtures, amd_smi=True):
    files = {("amd-smi", "metric"): "amd_smi_metric.json", ("amd-smi", "process"): "amd_smi_process.json",
             ("amd-smi", "static"): "amd_smi_static.json", ("rocm-smi", "--showmeminfo"): "rocm_smi.json",
             ("docker", "ps"): "docker_ps.txt"}

    def run(cmd):
        if cmd[0] == "amd-smi" and not amd_smi:
            return None
        name = files.get((cmd[0], cmd[1]))
        return (bench_fixtures / name).read_text(encoding="utf-8") if name else None

    return run


def test_sample_once_and_run_write_csv_and_summary(tmp_path, bench_fixtures):
    proc = _fake_proc(tmp_path, bench_fixtures)
    snap = gpu_telemetry.sample_once(_runner(bench_fixtures), proc_root=proc)
    assert snap.source == "amd-smi"
    assert [p.container for p in snap.processes] == ["wapsi-amd-llm-1", "wapsi-amd-verifier-llm-1", "host"]

    out = tmp_path / "telemetry"
    summary = gpu_telemetry.run(out, 0.0, None, runner=_runner(bench_fixtures), sleep=lambda s: None, max_samples=3,
                                proc_root=proc)
    assert summary["samples"] == 3 and summary["source"] == "amd-smi"
    (g,) = summary["gpus"]
    assert g["name"] == "AMD Instinct MI300X" and g["vram_used_mb_peak"] == 142000.0
    assert g["vram_used_pct_peak"] == pytest.approx(72.2, abs=0.1)
    owners = {p["owner"]: p["vram_mb_peak"] for p in summary["processes"]}
    assert owners == {"wapsi-amd-llm-1": 102400.0, "wapsi-amd-verifier-llm-1": 27648.0, "host": 4096.0}
    assert len((out / "telemetry.csv").read_text().splitlines()) == 4  # header + 3 samples
    assert len((out / "telemetry_processes.csv").read_text().splitlines()) == 10
    assert json.loads((out / "telemetry_summary.json").read_text())["kind"] == "gpu_telemetry"


def test_sample_once_falls_back_to_rocm_smi_then_none(bench_fixtures):
    snap = gpu_telemetry.sample_once(_runner(bench_fixtures, amd_smi=False))
    assert snap.source == "rocm-smi" and snap.gpus[0].gfx_activity_pct == 35.0 and snap.processes == []
    assert gpu_telemetry.sample_once(lambda cmd: None).source == "none"


# --------------------------------------------------------------------------- end-to-end bench (FakeLLM)


def _telemetry_summary(tmp_path, bench_fixtures) -> dict:
    snaps = [gpu_telemetry.sample_once(_runner(bench_fixtures), proc_root=_fake_proc(tmp_path, bench_fixtures))]
    return gpu_telemetry.summarize(snaps, 2.0, {0: "AMD Instinct MI300X"})


def _llm_bench_result() -> dict:
    cfg = llm_bench.BenchConfig(base_url="http://llm.test/v1", levels=(1, 2), requests=2, output_len=4, warmup=0)
    return asyncio.run(llm_bench.run_bench(cfg, transport=_fake_openai(ttft=0.0, itl=0.0, tokens=4)))


@pytest.fixture()
def bench_inputs(tmp_path, bench_fixtures):
    tele = common.write_json(tmp_path / "in" / "telemetry_summary.json", _telemetry_summary(tmp_path, bench_fixtures))
    lb = common.write_json(tmp_path / "in" / "llm_bench.json", _llm_bench_result())
    return tele, lb


@pytest.mark.parametrize("concurrency", [1, 2])
def test_e2e_bench_on_showcase_with_fake_llm(tmp_path, bench_inputs, concurrency, capsys):
    tele, lb = bench_inputs
    out = tmp_path / f"results-{concurrency}"
    code = e2e_bench.main(["--world", "showcase-memory", "--fake-llm", "--no-search", "--concurrency",
                           str(concurrency), "--out", str(out), "--telemetry", str(tele), "--llm-bench", str(lb),
                           "--inr-per-usd", "80", "--store-dir", str(tmp_path / "store")])
    assert code == 0, capsys.readouterr().out
    rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
    m, c = rep["metrics"], rep["cost"]
    assert rep["kind"] == "e2e_bench" and rep["world"] == "showcase-memory"
    assert (m["cases"], m["cases_ok"], m["cases_failed"]) == (2, 2, 0)
    assert m["cases_per_hour"] > 0 and m["wall_s"] > 0
    assert m["time_to_hold_plan_s"]["p50"] > 0 and m["case_total_s"]["p95"] >= m["case_total_s"]["p50"]
    assert m["draft_verification_pass_rate"] == 1.0 and m["cases_all_verified_rate"] == 1.0
    assert m["prose_fallback_rate"] == 0.0 and m["llm_drafts"] > 0
    assert m["hold_exact_match_rate"] == 1.0
    assert m["llm_calls"] > 0 and m["llm_errors"] == 0 and m["llm_calls_per_case"] > 0
    assert c["gpu_hour_usd"] == 1.99
    assert c["usd_per_case"] == pytest.approx(1.99 / m["cases_per_hour"], rel=1e-3)
    assert c["inr_per_case"] == pytest.approx(c["usd_per_case"] * 80, rel=1e-3)
    assert rep["settings"]["concurrency"] == concurrency
    assert rep["model"]["llm"].startswith("fake-llm") and rep["model"]["scorer"] == "heuristic-v2"
    mem = rep["memory"]
    assert mem["gpu_name"] == "AMD Instinct MI300X"
    assert {p["owner"] for p in mem["per_owner"]} == {"wapsi-amd-llm-1", "wapsi-amd-verifier-llm-1", "host"}
    assert [r["concurrency"] for r in rep["serving"]] == [1, 2]
    assert len(rep["cases"]) == 2 and rep["failures"] == []
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "Offline harness check" in md  # FakeLLM runs are labelled, never mistaken for GPU numbers
    for heading in ("## Model", "## Throughput and cost", "Cases/hour", "₹ per case", "## GPU memory split",
                    "## Serving (llm_bench)", "## Quality (verifier checks on every draft)"):
        assert heading in md
    from editions.amd.app.file_store import FileCaseStore

    assert len(FileCaseStore(tmp_path / "store").list_cases()) == 2  # bundles persisted for the UI


def test_e2e_bench_counts_failed_cases(tmp_path):
    from editions.amd.app.services import Services
    from editions.amd.app.settings import AmdSettings
    from wapsi.adapters.memory import RecordingActionSink
    from wapsi.verify.injection import RulesGuard

    class Broken:
        name = "broken"

        def complete(self, *a, **k):
            raise RuntimeError("model crashed")

    llm = e2e_bench.TimedLLM(Broken())
    svc = Services(settings=AmdSettings(demo_mode=True), ledger=e2e_bench.load_world("showcase-memory"), store=None,
                   llm=llm, guard=RulesGuard(), search=None, sink=RecordingActionSink())
    golden = e2e_bench.load_golden("showcase-memory")
    rows, wall = e2e_bench.run_cases(svc, golden, 1, llm)
    rep = e2e_bench.build_report(rows, wall, world="showcase-memory", concurrency=1, llm=llm, model_info={},
                                 gpu_hour_usd=1.99, inr_per_usd=None)
    m = rep["metrics"]
    # prose failures fall back to the deterministic paragraph, so cases still complete and verify
    assert m["cases_ok"] == 2 and m["prose_fallback_rate"] == 1.0 and m["llm_errors"] == m["llm_calls"] > 0
    assert rep["cost"]["inr_per_case"] is None


# --------------------------------------------------------------------------- merged report


def test_report_merges_all_inputs(tmp_path, bench_inputs):
    tele, lb = bench_inputs
    rdir = tmp_path / "results"
    assert e2e_bench.main(["--world", "showcase-memory", "--fake-llm", "--no-search", "--out", str(rdir),
                           "--inr-per-usd", "80"]) == 0
    shutil.copy(tele, rdir / "telemetry_summary.json")
    shutil.copy(lb, rdir / "llm_bench.json")
    common.write_json(rdir / "gnn_metrics.json", {"kind": "gnn_train", "metrics": {"epoch_time_s": 1.5,
                                                                                  "val_f1": 0.9}})
    common.write_json(rdir / "lora_eval.json", {"base": {"verifier_pass_rate": 0.8}, "tuned": {"verifier_pass_rate": 0.95}})
    assert report.main(["--results-dir", str(rdir)]) == 0
    md = (rdir / "AMD_RESULTS.md").read_text(encoding="utf-8")
    for needle in ("# Wapsi on AMD Instinct MI300X", "## Headline", "## Serving: `wapsi-llm`", "## End to end",
                   "## GPU memory and utilisation", "Peak VRAM per container", "wapsi-amd-llm-1",
                   "AMD Instinct MI300X", "## GNN (GraphSAGE on ROCm): `gnn_metrics.json`", "epoch_time_s",
                   "## LoRA fine-tune (Gemma 4, merged): `lora_eval.json`", "tuned.verifier_pass_rate",
                   "Cost per case (INR", "## Sources", "offline harness check"):
        assert needle in md, needle
    found = report.discover(rdir)
    assert [p.name for p in found["e2e"]] == ["report.json"]


def test_report_requires_inputs(tmp_path):
    with pytest.raises(SystemExit):
        report.main([])
