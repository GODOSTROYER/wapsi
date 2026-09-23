"""GPU telemetry sampler for the MI300X droplet (standard library only; run it on the HOST, not in a container).

    python3 editions/amd/bench/gpu_telemetry.py --once                     # one snapshot (good on camera)
    python3 editions/amd/bench/gpu_telemetry.py --interval 2 --duration 900 --out editions/amd/bench/results/<stamp>
    python3 editions/amd/bench/gpu_telemetry.py --interval 2 --out ... &   # until Ctrl-C / kill -INT

Every ``--interval`` seconds it runs ``amd-smi metric --json`` (fallback: ``rocm-smi --showmeminfo vram --showuse
--json``) and ``amd-smi process --json``, maps GPU processes to Docker containers (``/proc/<pid>/cgroup`` +
``docker ps``) and writes:

    telemetry.csv                one row per GPU per sample (VRAM used/total MB, GFX / memory activity %, power W)
    telemetry_processes.csv      one row per GPU process per sample (pid, name, container, VRAM MB)
    telemetry_summary.json       peaks/means per GPU and peak VRAM per container (-> e2e_bench / report.py)

The JSON shapes differ between amd-smi releases (list vs ``{"gpu_data": [...]}``, ``{"value", "unit"}`` objects vs
plain numbers, upper/lower-case keys). The parsers accept all of those; the fixtures in ``bench/fixtures/`` are
hand-written from the documented shape with synthetic values. **Confirm the exact shape on the droplet** once
(``amd-smi metric --json > /tmp/m.json; amd-smi process --json > /tmp/p.json``), replace the fixtures with those
captures, and rerun ``uv run pytest tests/amd -k telemetry``.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MB = 1024 * 1024
RESULTS_ROOT = Path(__file__).resolve().parent / "results"

Runner = Callable[[list[str]], str | None]


@dataclass
class GpuSample:
    gpu: int
    vram_used_mb: float | None = None
    vram_total_mb: float | None = None
    gfx_activity_pct: float | None = None
    umc_activity_pct: float | None = None
    power_w: float | None = None
    temp_hotspot_c: float | None = None
    temp_mem_c: float | None = None


@dataclass
class ProcSample:
    gpu: int
    pid: int
    name: str = ""
    vram_mb: float | None = None
    container: str | None = None


@dataclass
class Snapshot:
    ts: str
    source: str
    gpus: list[GpuSample] = field(default_factory=list)
    processes: list[ProcSample] = field(default_factory=list)


# --------------------------------------------------------------------------- value helpers

_UNIT_TO_MB = {"b": 1 / MB, "kb": 1 / 1024, "kib": 1 / 1024, "mb": 1.0, "mib": 1.0, "gb": 1024.0, "gib": 1024.0}


def _lower(obj: Any) -> Any:
    """Recursively lower-case dict keys (amd-smi text output uses upper case, JSON lower case)."""
    if isinstance(obj, dict):
        return {str(k).lower(): _lower(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_lower(v) for v in obj]
    return obj


def _num(v: Any) -> float | None:
    """Number from 42, "42", "42 %", {"value": 42, "unit": "%"}; None for "N/A" and friends."""
    if isinstance(v, dict):
        v = v.get("value")
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        return float(v)
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)", str(v))
    return float(m.group(1)) if m else None


def _mb(v: Any, default_unit: str = "mb") -> float | None:
    unit = default_unit
    if isinstance(v, dict):
        unit = str(v.get("unit") or default_unit)
    elif isinstance(v, str):
        m = re.match(r"^\s*-?\d+(?:\.\d+)?\s*([A-Za-z]+)", v)
        if m:
            unit = m.group(1)
    n = _num(v)
    if n is None:
        return None
    return round(n * _UNIT_TO_MB.get(unit.lower(), 1.0), 3)


def _gpu_entries(data: Any) -> list[dict[str, Any]]:
    data = _lower(data)
    if isinstance(data, dict):
        for key in ("gpu_data", "gpus", "data"):
            if isinstance(data.get(key), list):
                return [d for d in data[key] if isinstance(d, dict)]
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "N/A", "n/a"):
            return d[k]
    return None


# --------------------------------------------------------------------------- parsers


def parse_amd_smi_metric(data: Any) -> list[GpuSample]:
    """``amd-smi metric --json`` -> one GpuSample per GPU."""
    out = []
    for i, e in enumerate(_gpu_entries(data)):
        usage = e.get("usage") or {}
        mem = e.get("mem_usage") or e.get("memory_usage") or {}
        power = e.get("power") or {}
        temp = e.get("temperature") or {}
        gfx = _first(usage, "gfx_activity", "gfx_usage", "gfx_busy_inst")
        if isinstance(gfx, list):  # some releases report per-XCP lists
            vals = [x for x in (_num(g) for g in gfx) if x is not None]
            gfx = sum(vals) / len(vals) if vals else None
        out.append(GpuSample(
            gpu=int(_num(e.get("gpu")) if _num(e.get("gpu")) is not None else i),
            vram_used_mb=_mb(_first(mem, "used_vram", "vram_used")),
            vram_total_mb=_mb(_first(mem, "total_vram", "vram_total")),
            gfx_activity_pct=_num(gfx),
            umc_activity_pct=_num(_first(usage, "umc_activity", "mem_activity")),
            power_w=_num(_first(power, "socket_power", "average_socket_power", "current_socket_power")),
            temp_hotspot_c=_num(_first(temp, "hotspot", "junction", "edge")),
            temp_mem_c=_num(_first(temp, "mem", "memory")),
        ))
    return out


def parse_amd_smi_process(data: Any) -> list[ProcSample]:
    """``amd-smi process --json`` -> one ProcSample per GPU process (empty list when none run)."""
    out = []
    for i, e in enumerate(_gpu_entries(data)):
        gpu = int(_num(e.get("gpu")) if _num(e.get("gpu")) is not None else i)
        plist = e.get("process_list", e.get("processes", []))
        if not isinstance(plist, list):  # "No running processes detected"
            continue
        for item in plist:
            if not isinstance(item, dict):
                continue
            info = item.get("process_info", item)
            if not isinstance(info, dict):  # "No running processes detected" inside the list
                continue
            pid = _num(info.get("pid"))
            if pid is None:
                continue
            mem = info.get("memory_usage") or {}
            vram = _first(mem, "vram_mem", "vram") if isinstance(mem, dict) else None
            if vram is None:
                vram = info.get("mem_usage", info.get("mem"))
            out.append(ProcSample(gpu=gpu, pid=int(pid), name=str(info.get("name") or ""),
                                  vram_mb=_mb(vram, default_unit="b")))
    return out


def parse_rocm_smi(data: Any) -> list[GpuSample]:
    """``rocm-smi --showmeminfo vram --showuse --json`` -> GpuSamples (``card0``, ``card1``...)."""
    out = []
    if not isinstance(data, dict):
        return out
    for key, d in data.items():
        m = re.match(r"card(\d+)$", str(key))
        if not m or not isinstance(d, dict):
            continue
        lk = {str(k).lower(): v for k, v in d.items()}
        used = _num(lk.get("vram total used memory (b)"))
        total = _num(lk.get("vram total memory (b)"))
        out.append(GpuSample(
            gpu=int(m.group(1)),
            vram_used_mb=round(used / MB, 3) if used is not None else None,
            vram_total_mb=round(total / MB, 3) if total is not None else None,
            gfx_activity_pct=_num(lk.get("gpu use (%)")),
            umc_activity_pct=_num(lk.get("gpu memory use (%)") or lk.get("memory activity")),
            power_w=_num(lk.get("average graphics package power (w)") or lk.get("current socket graphics package power (w)")),
        ))
    return sorted(out, key=lambda g: g.gpu)


def parse_amd_smi_static(data: Any) -> dict[int, str]:
    """``amd-smi static --asic --json`` -> {gpu: market name}."""
    out = {}
    for i, e in enumerate(_gpu_entries(data)):
        asic = e.get("asic") or {}
        name = asic.get("market_name") if isinstance(asic, dict) else None
        gpu = int(_num(e.get("gpu")) if _num(e.get("gpu")) is not None else i)
        if name:
            out[gpu] = str(name)
    return out


_CID = re.compile(r"(?:docker[-/]|/docker/|containerd[-/:]|cri-containerd-)([0-9a-f]{64})")


def container_id_from_cgroup(text: str) -> str | None:
    """Docker container id from ``/proc/<pid>/cgroup`` (cgroup v1 or v2)."""
    for line in text.splitlines():
        m = _CID.search(line)
        if m:
            return m.group(1)
    return None


def parse_docker_ps(text: str) -> dict[str, str]:
    """``docker ps --no-trunc --format '{{.ID}} {{.Names}}'`` -> {full id: name}."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{12,64}", parts[0]):
            out[parts[0]] = parts[1]
    return out


def map_containers(procs: Iterable[ProcSample], names: dict[str, str], proc_root: Path = Path("/proc")) -> None:
    for p in procs:
        try:
            cid = container_id_from_cgroup((proc_root / str(p.pid) / "cgroup").read_text(encoding="utf-8"))
        except OSError:
            cid = None
        if cid:
            p.container = names.get(cid) or next((n for i, n in names.items() if cid.startswith(i)), cid[:12])
        else:
            p.container = "host"


# --------------------------------------------------------------------------- sampling


def run_cmd(cmd: list[str], timeout: float = 15.0) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 and res.stdout.strip() else None


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)  # banner before JSON
        return json.loads(text[start:]) if start >= 0 else None


def sample_once(runner: Runner = run_cmd, proc_root: Path = Path("/proc"), map_docker: bool = True) -> Snapshot:
    ts = datetime.now(UTC).isoformat()
    metric = _json(runner(["amd-smi", "metric", "--json"]))
    if metric is not None:
        snap = Snapshot(ts=ts, source="amd-smi", gpus=parse_amd_smi_metric(metric))
        procs = _json(runner(["amd-smi", "process", "--json"]))
        snap.processes = parse_amd_smi_process(procs) if procs is not None else []
    else:
        legacy = _json(runner(["rocm-smi", "--showmeminfo", "vram", "--showuse", "--showpower", "--json"]))
        if legacy is None:
            return Snapshot(ts=ts, source="none")
        snap = Snapshot(ts=ts, source="rocm-smi", gpus=parse_rocm_smi(legacy))
    if map_docker and snap.processes:
        names = parse_docker_ps(runner(["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"]) or "")
        map_containers(snap.processes, names, proc_root)
    return snap


def summarize(snaps: list[Snapshot], interval_s: float, gpu_names: dict[int, str] | None = None) -> dict[str, Any]:
    gpus: dict[int, list[GpuSample]] = {}
    for s in snaps:
        for g in s.gpus:
            gpus.setdefault(g.gpu, []).append(g)

    def stats(vals: list[float | None]) -> tuple[float | None, float | None]:
        v = [x for x in vals if x is not None]
        return (round(sum(v) / len(v), 2), round(max(v), 2)) if v else (None, None)

    gpu_rows = []
    for gpu, rows in sorted(gpus.items()):
        used_mean, used_peak = stats([r.vram_used_mb for r in rows])
        gfx_mean, gfx_peak = stats([r.gfx_activity_pct for r in rows])
        pw_mean, pw_peak = stats([r.power_w for r in rows])
        total = next((r.vram_total_mb for r in rows if r.vram_total_mb), None)
        gpu_rows.append({
            "gpu": gpu, "name": (gpu_names or {}).get(gpu), "samples": len(rows), "vram_total_mb": total,
            "vram_used_mb_mean": used_mean, "vram_used_mb_peak": used_peak,
            "vram_used_pct_peak": round(100 * used_peak / total, 1) if used_peak and total else None,
            "gfx_activity_pct_mean": gfx_mean, "gfx_activity_pct_peak": gfx_peak,
            "power_w_mean": pw_mean, "power_w_peak": pw_peak,
        })
    by_owner: dict[tuple[int, str], dict[str, Any]] = {}
    for s in snaps:
        per: dict[tuple[int, str], float] = {}
        names: dict[tuple[int, str], set[str]] = {}
        for p in s.processes:
            key = (p.gpu, p.container or p.name or str(p.pid))
            per[key] = per.get(key, 0.0) + (p.vram_mb or 0.0)
            names.setdefault(key, set()).add(p.name)
        for key, mb in per.items():
            row = by_owner.setdefault(key, {"gpu": key[0], "owner": key[1], "process_names": set(),
                                            "vram_mb_peak": 0.0})
            row["vram_mb_peak"] = round(max(row["vram_mb_peak"], mb), 1)
            row["process_names"] |= names[key]
    processes = [{**r, "process_names": sorted(r["process_names"])}
                 for r in sorted(by_owner.values(), key=lambda r: (r["gpu"], -r["vram_mb_peak"]))]
    return {
        "kind": "gpu_telemetry",
        "schema_version": 1,
        "source": next((s.source for s in snaps if s.source != "none"), "none"),
        "interval_s": interval_s,
        "samples": len(snaps),
        "started_at": snaps[0].ts if snaps else None,
        "ended_at": snaps[-1].ts if snaps else None,
        "gpus": gpu_rows,
        "processes": processes,
    }


class _Stop:
    flag = False


def run(out: Path, interval_s: float, duration_s: float | None, runner: Runner = run_cmd,
        sleep: Callable[[float], None] = time.sleep, max_samples: int | None = None,
        proc_root: Path = Path("/proc")) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    names = parse_amd_smi_static(_json(runner(["amd-smi", "static", "--asic", "--json"])) or [])
    snaps: list[Snapshot] = []
    t0 = time.monotonic()
    gpu_csv = (out / "telemetry.csv").open("w", newline="", encoding="utf-8")
    proc_csv = (out / "telemetry_processes.csv").open("w", newline="", encoding="utf-8")
    gw = csv.DictWriter(gpu_csv, fieldnames=["ts", "source", *GpuSample.__dataclass_fields__])
    pw = csv.DictWriter(proc_csv, fieldnames=["ts", *ProcSample.__dataclass_fields__])
    gw.writeheader()
    pw.writeheader()
    try:
        while not _Stop.flag:
            s = sample_once(runner, proc_root=proc_root)
            snaps.append(s)
            for g in s.gpus:
                gw.writerow({"ts": s.ts, "source": s.source, **asdict(g)})
            for p in s.processes:
                pw.writerow({"ts": s.ts, **asdict(p)})
            gpu_csv.flush()
            proc_csv.flush()
            if max_samples is not None and len(snaps) >= max_samples:
                break
            if duration_s is not None and time.monotonic() - t0 >= duration_s:
                break
            sleep(interval_s)
    finally:
        gpu_csv.close()
        proc_csv.close()
        summary = summarize(snaps, interval_s, names)
        summary["files"] = {"gpu_csv": "telemetry.csv", "process_csv": "telemetry_processes.csv"}
        (out / "telemetry_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def print_snapshot(s: Snapshot) -> None:
    print(f"{s.ts}  source={s.source}")
    for g in s.gpus:
        print(f"  GPU{g.gpu}: VRAM {g.vram_used_mb} / {g.vram_total_mb} MB  GFX {g.gfx_activity_pct}%  "
              f"MEM {g.umc_activity_pct}%  {g.power_w} W  hotspot {g.temp_hotspot_c} C")
    for p in s.processes:
        print(f"    pid {p.pid:<8} {p.name:<24} {p.container or '':<24} {p.vram_mb} MB")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between samples")
    ap.add_argument("--duration", type=float, default=None, help="stop after this many seconds (default: Ctrl-C)")
    ap.add_argument("--out", default=None, help="results directory (default bench/results/<utc stamp>)")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    args = ap.parse_args(argv)
    if args.once:
        snap = sample_once()
        print_snapshot(snap)
        return 0 if snap.source != "none" else 1
    out = Path(args.out) if args.out else RESULTS_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    def stop(*_: object) -> None:
        _Stop.flag = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"sampling every {args.interval}s into {out} (Ctrl-C to stop)", flush=True)
    summary = run(out, args.interval, args.duration)
    print(json.dumps({k: summary[k] for k in ("source", "samples", "gpus")}, indent=2))
    return 0 if summary["source"] != "none" else 1


if __name__ == "__main__":
    sys.exit(main())
