"""GPU telemetry on an SR-IOV virtual function: unavailable utilisation / power sensors read "n/a (SR-IOV VF)", not 0.

The amd-smi payloads below are synthetic, in the documented JSON shape (like ``editions/amd/bench/fixtures``).
"""

from __future__ import annotations

import json

import pytest

from editions.amd.bench import gpu_telemetry, report
from editions.amd.bench.gpu_telemetry import SRIOV_NA, GpuSample, Snapshot

VF_STATIC = [{"gpu": 0, "asic": {"market_name": "AMD Instinct MI300X VF", "vendor_id": "0x1002",
                                 "device_id": "0x74b5"}}]  # fmt: skip
VF_METRIC = {"gpu_data": [{
    "gpu": 0,
    "usage": {"gfx_activity": "N/A", "umc_activity": {"value": 0, "unit": "%"}},
    "power": {"socket_power": {"value": 0, "unit": "W"}},
    "temperature": {"hotspot": "N/A"},
    "mem_usage": {"total_vram": {"value": 196592, "unit": "MB"}, "used_vram": {"value": 150000, "unit": "MB"}},
}]}  # fmt: skip


def _runner(static=VF_STATIC, metric=VF_METRIC):
    def run(cmd):
        if cmd[:2] == ["amd-smi", "static"]:
            return json.dumps(static)
        if cmd[:2] == ["amd-smi", "metric"]:
            return json.dumps(metric)
        if cmd[:2] == ["amd-smi", "process"]:
            return json.dumps({"gpu_data": [{"gpu": 0, "process_list": []}]})
        return None

    return run


def _snap(**kw) -> Snapshot:
    return Snapshot(ts="2026-10-12T10:00:00+00:00", source="amd-smi", gpus=[GpuSample(gpu=0, **kw)])


def test_vf_detection_from_market_name_or_device_id():
    assert gpu_telemetry.parse_sriov_vfs(VF_STATIC) == {0}
    by_id = [{"gpu": 1, "asic": {"market_name": "AMD Instinct", "device_id": "0x74B5"}}]
    assert gpu_telemetry.parse_sriov_vfs(by_id) == {1}
    pf = [{"gpu": 0, "asic": {"market_name": "AMD Instinct MI300X", "device_id": "0x74a1"}}]
    assert gpu_telemetry.parse_sriov_vfs(pf) == set()
    assert gpu_telemetry.parse_sriov_vfs([{"gpu": 0, "asic": "N/A"}]) == set()


def test_sensor_text():
    assert gpu_telemetry.sensor_text(None) == "n/a"
    assert gpu_telemetry.sensor_text(None, vf=True) == SRIOV_NA
    assert gpu_telemetry.sensor_text(0.0, vf=True, unit="%") == SRIOV_NA
    assert gpu_telemetry.sensor_text(0.0, unit="%") == "0%"  # a physical GPU's 0 is a reading
    assert gpu_telemetry.sensor_text(512.0, vf=True, unit=" W") == "512 W"


def test_summary_never_reports_an_unavailable_vf_sensor_as_zero():
    snaps = [_snap(vram_used_mb=100.0, vram_total_mb=1000.0, gfx_activity_pct=None, power_w=0.0)] * 3
    (g,) = gpu_telemetry.summarize(snaps, 2.0, vf_gpus={0})["gpus"]
    assert g["sriov_vf"] is True and g["unavailable_sensors"] == ["gfx_activity_pct", "power_w"]
    assert g["gfx_activity_pct_mean"] is None and g["power_w_mean"] is None and g["power_w_peak"] is None
    assert g["vram_used_mb_peak"] == 100.0  # VRAM is still measured on a VF
    # the same readings on a physical GPU: the missing sensor is unavailable, the zero is a reading
    (p,) = gpu_telemetry.summarize(snaps, 2.0)["gpus"]
    assert (
        p["sriov_vf"] is False
        and p["unavailable_sensors"] == ["gfx_activity_pct"]
        and p["power_w_mean"] == 0.0
    )
    # a VF sensor that reads above zero at least once works: its numbers are kept
    live = [_snap(gfx_activity_pct=0.0, power_w=0.0), _snap(gfx_activity_pct=40.0, power_w=300.0)]
    (v,) = gpu_telemetry.summarize(live, 2.0, vf_gpus={0})["gpus"]
    assert v["unavailable_sensors"] == [] and (v["gfx_activity_pct_mean"], v["power_w_peak"]) == (20.0, 300.0)


def test_snapshot_printout_on_a_vf(capsys):
    gpu_telemetry.print_snapshot(_snap(vram_used_mb=10.0, vram_total_mb=20.0, gfx_activity_pct=0.0,
                                       umc_activity_pct=None, power_w=0.0), vf_gpus={0})  # fmt: skip
    out = capsys.readouterr().out
    assert "GPU0 (SR-IOV VF)" in out and f"GFX {SRIOV_NA}" in out and f"power {SRIOV_NA}" in out
    assert "0%" not in out and "None" not in out and " 0 W" not in out
    gpu_telemetry.print_snapshot(_snap(gfx_activity_pct=35.0, power_w=420.0))
    out = capsys.readouterr().out
    assert "GFX 35%" in out and "power 420 W" in out and "SR-IOV" not in out


@pytest.mark.parametrize("forced", [False, True])
def test_run_and_report_on_a_vf(tmp_path, forced):
    static = VF_STATIC if not forced else [{"gpu": 0, "asic": {"market_name": "AMD Instinct MI300X"}}]
    summary = gpu_telemetry.run(tmp_path / "t", 0.0, None, runner=_runner(static), sleep=lambda s: None,
                                max_samples=2, proc_root=tmp_path, sriov_vf=forced)  # fmt: skip
    (g,) = summary["gpus"]
    assert g["sriov_vf"] is True and g["gfx_activity_pct_mean"] is None and g["power_w_mean"] is None
    saved = json.loads((tmp_path / "t" / "telemetry_summary.json").read_text(encoding="utf-8"))
    assert saved["gpus"][0]["unavailable_sensors"] == ["gfx_activity_pct", "power_w"]
    md = report.render([], [], [{**saved, "_path": "telemetry_summary.json"}], [], [])
    row = next(line for line in md.splitlines() if line.startswith("| 0 |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[5:8] == [SRIOV_NA, SRIOV_NA, SRIOV_NA]  # GFX mean, GFX peak, power mean
    assert cells[3] == "146.5"  # peak VRAM (GB) is still reported


def test_report_labels_a_vf_zero_in_an_older_summary():
    old = {"_path": "old.json", "source": "amd-smi", "samples": 1, "interval_s": 2,
           "gpus": [{"gpu": 0, "sriov_vf": True, "gfx_activity_pct_mean": 0.0, "gfx_activity_pct_peak": 0.0,
                     "power_w_mean": 0.0, "vram_total_mb": None}]}  # fmt: skip
    md = report.render([], [], [old], [], [])
    row = next(line for line in md.splitlines() if line.startswith("| 0 |"))
    assert row.count(SRIOV_NA) == 3 and "| n/a |" in row
