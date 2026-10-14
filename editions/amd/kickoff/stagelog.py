"""Stage bookkeeping for ``run_all.sh`` (standard library only: runs with the droplet's system ``python3``).

    python3 editions/amd/kickoff/stagelog.py window                    # exit 0 inside the event window, 3 before, 4 after
    python3 editions/amd/kickoff/stagelog.py budget --evid DIR --mode gpu [--stage NAME] [--quiet]
    python3 editions/amd/kickoff/stagelog.py snapshot OUT.json         # amd-smi metric/process (rocm-smi fallback)
    python3 editions/amd/kickoff/stagelog.py static OUT.json           # amd-smi static: GPU name, VF, ROCm version
    python3 editions/amd/kickoff/stagelog.py record --evid DIR --index 3 --stage gnn --status ok ...
    python3 editions/amd/kickoff/stagelog.py wait-http URL --timeout 1800
    python3 editions/amd/kickoff/stagelog.py free-port
    python3 editions/amd/kickoff/stagelog.py detect-mode               # droplet | notebook | endpoint (exit 1: none)
    python3 editions/amd/kickoff/stagelog.py torch-info OUT.json       # torch / ROCm (HIP) / GPU name and memory

Modes. ``droplet`` (``gpu`` is the old name): an AMD GPU and Docker (the AMD Developer Cloud MI300X droplet).
``notebook``: an AMD GPU without Docker (an AMD ROCm JupyterLab). ``endpoint``: no GPU box; the LLM side runs against
``WAPSI_LLM_BASE_URL`` (AMD Token Factory or anyone's vLLM). ``rehearsal``: a laptop CPU, stub models, never a result.
``detect-mode`` looks at this machine (``amd-smi`` / ``rocm-smi`` / a ROCm torch, ``docker``, the endpoint
variables); ``WAPSI_MODE`` overrides it.

Budget (the time/cost guard). The droplet bills from creation until it is destroyed, powered off or not, so spend is
``hours since the droplet started x $/GPU-hour`` plus what earlier droplets cost. The start time comes from
``WAPSI_DROPLET_STARTED_AT`` (ISO UTC, recommended: copy it from the console), else the machine's boot time
(``/proc/stat`` btime, close to droplet creation), else the first time ``run_all.sh`` ran (``budget_start.json``).
``WAPSI_SPENT_USD`` adds earlier droplets. ``budget`` exits 0 (ok), 10 (at or above ``WAPSI_BUDGET_WARN_PCT``,
default 75) or 20 (at or above ``WAPSI_BUDGET_STOP_PCT``, default 90: run_all.sh refuses to start a stage).
In the CPU rehearsal the same arithmetic runs on the rehearsal's own clock and is labelled hypothetical. Endpoint
mode has no GPU-hour bill (the API's own daily cost cap applies; ``endpoint.py`` reads it), and a notebook is
metered only when ``WAPSI_GPU_HOUR_USD`` says what its credits cost per hour: both then report "not metered" and
never stop a stage.

``WAPSI_NOW`` (ISO UTC) overrides the clock for tests; nothing else reads it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from editions.amd.bench import gpu_telemetry as tel  # noqa: E402  (standard library only)

SCHEMA = "wapsi-amd-stage/1"
# lablab.ai AMD Developer Hackathon ACT III: GPU runs and every reported GPU number fall inside this window
EVENT_START = "2026-10-12T15:00:00Z"
EVENT_END = "2026-10-18T14:00:00Z"
DEFAULT_GPU_HOUR_USD = 1.99
DEFAULT_BUDGET_USD = 100.0
DEFAULT_WARN_PCT = 75.0
DEFAULT_STOP_PCT = 90.0
EXIT_WARN, EXIT_STOP = 10, 20
EXIT_BEFORE, EXIT_AFTER = 3, 4
MODES = ("droplet", "notebook", "endpoint", "rehearsal")
MODE_CHOICES = (*MODES, "gpu")  # "gpu": the droplet's old name
RUN_LABELS = {
    "droplet": "MI300X run",
    "notebook": "AMD ROCm notebook run",
    "endpoint": "endpoint run (LLM calls served by the provider's GPUs; no GPU on this machine)",
    "rehearsal": "CPU rehearsal (tiny configs, stub LLMs): not a GPU result",
}


def canon_mode(mode: str) -> str:
    """``gpu`` (the droplet's old name) -> ``droplet``; the other names unchanged."""
    return "droplet" if mode == "gpu" else mode


def parse_ts(text: str) -> datetime:
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> datetime:
    override = os.environ.get("WAPSI_NOW")
    return parse_ts(override) if override else datetime.now(UTC)


def window_state(at: datetime | None = None) -> str:
    """``before`` | ``inside`` | ``after`` the event window."""
    at = at or now()
    if at < parse_ts(EVENT_START):
        return "before"
    if at > parse_ts(EVENT_END):
        return "after"
    return "inside"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from exc


def boot_time(proc_stat: Path = Path("/proc/stat")) -> datetime | None:
    try:
        for line in proc_stat.read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return datetime.fromtimestamp(int(line.split()[1]), UTC)
    except (OSError, ValueError, IndexError):
        return None
    return None


def budget_start(evid: Path, mode: str, *, proc_stat: Path = Path("/proc/stat")) -> tuple[datetime, str]:
    """When billing started, and where that came from (see the module docstring)."""
    env = os.environ.get("WAPSI_DROPLET_STARTED_AT", "").strip()
    if env:
        return parse_ts(env), "WAPSI_DROPLET_STARTED_AT"
    if mode == "gpu":
        bt = boot_time(proc_stat)
        if bt is not None:
            return bt, "boot time (/proc/stat btime)"
    marker = evid / "budget_start.json"
    if marker.exists():
        try:
            return parse_ts(json.loads(marker.read_text(encoding="utf-8"))["started_at"]), "first run_all.sh run"
        except (ValueError, KeyError, OSError):
            pass
    started = now()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"started_at": iso(started)}) + "\n", encoding="utf-8", newline="\n")
    return started, "first run_all.sh run"


def budget(evid: Path, mode: str, *, proc_stat: Path = Path("/proc/stat")) -> dict[str, Any]:
    mode = canon_mode(mode)
    priced = bool(os.environ.get("WAPSI_GPU_HOUR_USD", "").strip())
    # endpoint: no GPU-hour bill at all; notebook: metered only when the operator says what an hour costs
    metered = mode in ("droplet", "rehearsal") or (mode == "notebook" and priced)
    rate = _env_float("WAPSI_GPU_HOUR_USD", DEFAULT_GPU_HOUR_USD) if metered else 0.0
    cap = _env_float("WAPSI_BUDGET_USD", DEFAULT_BUDGET_USD)
    before = _env_float("WAPSI_SPENT_USD", 0.0)
    warn_pct = _env_float("WAPSI_BUDGET_WARN_PCT", DEFAULT_WARN_PCT)
    stop_pct = _env_float("WAPSI_BUDGET_STOP_PCT", DEFAULT_STOP_PCT)
    started, source = budget_start(evid, "gpu" if mode in ("droplet", "notebook") else mode, proc_stat=proc_stat)
    hours = max(0.0, (now() - started).total_seconds() / 3600.0)
    droplet = hours * rate
    total = before + droplet
    pct = 100.0 * total / cap if cap > 0 else 0.0
    state = ("stop" if pct >= stop_pct else "warn" if pct >= warn_pct else "ok") if metered else "ok"
    label = {
        "droplet": "AMD Developer Cloud droplet",
        "notebook": "AMD ROCm notebook, credits at WAPSI_GPU_HOUR_USD per hour" if priced else
                    "AMD ROCm notebook: not metered (set WAPSI_GPU_HOUR_USD to what an hour of its credits costs)",
        "endpoint": "endpoint mode: no GPU-hour bill (the API's daily cost cap applies; see endpoint.json)",
        "rehearsal": "hypothetical: the CPU rehearsal is not billed",
    }[mode]
    return {
        "billed": mode == "droplet" or (mode == "notebook" and priced),
        "metered": metered,
        "mode": mode,
        "label": label,
        "gpu_hour_usd": rate,
        "budget_usd": cap,
        "spent_before_usd": round(before, 2),
        "droplet_started_at": iso(started),
        "started_source": source,
        "hours": round(hours, 3),
        "droplet_usd": round(droplet, 2),
        "total_usd": round(total, 2),
        "pct": round(pct, 1),
        "warn_pct": warn_pct,
        "stop_pct": stop_pct,
        "state": state,
    }


def budget_line(b: dict[str, Any], stage: str | None = None) -> str:
    if not b.get("metered", True):
        return f"[budget] {b['label']}"
    head = "[budget]" if b["billed"] else "[budget, hypothetical]"
    line = (f"{head} up {b['hours']:.2f} h x ${b['gpu_hour_usd']:.2f} = ${b['droplet_usd']:.2f}"
            f" (+ ${b['spent_before_usd']:.2f} earlier) = ${b['total_usd']:.2f} of ${b['budget_usd']:.2f}"
            f" ({b['pct']:.1f}%) - {b['state']}")
    if b["state"] == "warn":
        line += (f"\n[budget] WARNING: {b['pct']:.1f}% of the GPU budget is used. Finish the current work, push the "
                 "evidence, then DESTROY the droplet (powered off still bills)."
                 if b.get("mode", "droplet") != "notebook" else
                 f"\n[budget] WARNING: {b['pct']:.1f}% of the GPU budget is used. Finish the current work, push the "
                 "evidence, then stop and DELETE the notebook instance.")
    elif b["state"] == "stop":
        line += (f"\n[budget] STOP: {b['pct']:.1f}% >= {b['stop_pct']:.0f}%."
                 + (f" Not starting stage '{stage}'." if stage else "")
                 + " Push the evidence and DESTROY the droplet; --ignore-budget overrides.")
    return line


# --------------------------------------------------------------------------- GPU snapshots


def snapshot(runner: tel.Runner = tel.run_cmd, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """One amd-smi (or rocm-smi) snapshot: parsed per-GPU and per-process numbers plus the raw metric JSON."""
    snap = tel.sample_once(runner, proc_root=proc_root)
    if snap.source == "none":
        return {"available": False, "captured_at": snap.ts,
                "reason": "neither amd-smi nor rocm-smi answered (no AMD GPU on this machine)"}
    raw = tel._json(runner(["amd-smi", "metric", "--json"])) if snap.source == "amd-smi" else None
    return {
        "available": True,
        "captured_at": snap.ts,
        "source": snap.source,
        "gpus": [vars(g) for g in snap.gpus],
        "processes": [vars(p) for p in snap.processes],
        "raw_metric": raw,
    }


def static_info(runner: tel.Runner = tel.run_cmd) -> dict[str, Any]:
    """``amd-smi static`` (GPU names, SR-IOV VF), ``amd-smi version`` and the ROCm version file."""
    data = tel._json(runner(["amd-smi", "static", "--json"]))
    version = tel._json(runner(["amd-smi", "version", "--json"]))
    rocm = None
    for p in (Path("/opt/rocm/.info/version"), Path("/opt/rocm/.info/version-dev")):
        try:
            rocm = p.read_text(encoding="utf-8").strip()
            break
        except OSError:
            continue
    if data is None:
        return {"available": False, "captured_at": iso(now()), "rocm_version": rocm,
                "reason": "amd-smi static did not answer (no AMD GPU on this machine)"}
    names = tel.parse_amd_smi_static(data)
    vfs = tel.parse_sriov_vfs(data)
    return {
        "available": True,
        "captured_at": iso(now()),
        "gpus": [{"gpu": g, "name": n, "sriov_vf": g in vfs} for g, n in sorted(names.items())],
        "rocm_version": rocm,
        "amd_smi_version": version,
        "raw_static": data,
    }


# --------------------------------------------------------------------------- torch / mode detection


def torch_info() -> dict[str, Any]:
    """What the interpreter's torch sees: version, ROCm (HIP) build, the GPU's name and memory. Never raises."""
    out: dict[str, Any] = {"available": False, "captured_at": iso(now()), "python": sys.version.split()[0]}
    try:
        import torch  # noqa: PLC0415 - optional: the droplet's system python3 has none
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"torch not importable: {type(exc).__name__}"
        return out
    out.update(torch=str(torch.__version__), hip=getattr(torch.version, "hip", None), cuda=torch.version.cuda)
    try:
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            out.update(available=True, device_count=int(torch.cuda.device_count()),
                       device_name=str(torch.cuda.get_device_name(0)),
                       vram_total_mb=round(props.total_memory / (1024 * 1024)))
        else:
            out["reason"] = "torch sees no GPU (torch.cuda.is_available() is False)"
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"{type(exc).__name__}: {exc}"[:200]
    out["rocm_build"] = bool(out.get("hip"))
    return out


def detect_mode(env: dict[str, str] | None = None, *, runner: tel.Runner = tel.run_cmd,
                which: Any = shutil.which, torch_probe: Any = None) -> tuple[str, list[str]]:
    """(``droplet`` | ``notebook`` | ``endpoint`` | ``none``, why) for this machine.

    An AMD GPU (``amd-smi`` / ``rocm-smi`` answers, or a ROCm torch sees a device) with a reachable Docker daemon is
    the droplet; without Docker it is a notebook; no GPU but ``WAPSI_LLM_BASE_URL`` set is endpoint mode.
    ``WAPSI_MODE`` (or ``run_all.sh --mode``) overrides the detection."""
    env = dict(os.environ) if env is None else env
    forced = canon_mode((env.get("WAPSI_MODE") or "").strip().lower())
    if forced in MODES:
        return forced, [f"WAPSI_MODE={forced}"]
    why: list[str] = []
    snap = tel.sample_once(runner, map_docker=False)
    gpu = snap.source != "none" and bool(snap.gpus)
    if gpu:
        why.append(f"{snap.source} reports {len(snap.gpus)} GPU(s)")
    else:
        info = (torch_probe or torch_info)()
        gpu = bool(info.get("available") and info.get("rocm_build"))
        why.append(f"torch (ROCm {info.get('hip')}) sees {info.get('device_name')}" if gpu
                   else "no AMD GPU on this machine (amd-smi, rocm-smi and a ROCm torch all silent)")
    docker = bool(which("docker")) and bool((runner(["docker", "version", "--format", "{{.Server.Version}}"]) or "").strip())
    why.append("docker daemon reachable" if docker else "no docker daemon")
    url = (env.get("WAPSI_LLM_BASE_URL") or "").strip()
    if gpu:
        return ("droplet" if docker else "notebook"), why
    if url:
        why.append("WAPSI_LLM_BASE_URL is set")
        return "endpoint", why
    why.append("WAPSI_LLM_BASE_URL is not set")
    return "none", why


# --------------------------------------------------------------------------- stage records


def read_notes(path: Path | None) -> dict[str, str]:
    """``key=value`` lines a stage appended to its notes file (later lines win)."""
    out: dict[str, str] = {}
    if path is None or not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def record(evid: Path, *, index: int, stage: str, mode: str, status: str, exit_code: int, started: str, ended: str,
           log: str | None = None, before: str | None = None, after: str | None = None,
           notes: Path | None = None, budget_info: dict[str, Any] | None = None) -> Path:
    t0, t1 = parse_ts(started), parse_ts(ended)
    mode = canon_mode(mode)
    rec = {
        "schema": SCHEMA,
        "stage": stage,
        "index": index,
        "mode": mode,
        "label": RUN_LABELS[mode],
        "status": status,
        "exit_code": exit_code,
        "started_at": iso(t0),
        "ended_at": iso(t1),
        "duration_s": round((t1 - t0).total_seconds(), 1),
        "window": window_state(t0),
        "log": log,
        "amd_smi_before": before,
        "amd_smi_after": after,
        "notes": read_notes(notes),
        "budget": budget_info,
    }
    out = evid / "stages" / f"{index:02d}-{stage}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8", newline="\n")
    return out


def wait_http(url: str, timeout_s: float, interval_s: float = 10.0, api_key: str | None = None) -> bool:
    deadline = time.monotonic() + timeout_s
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write(path: str, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stagelog.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("window", help="exit 0 inside the event window, 3 before, 4 after")
    b = sub.add_parser("budget")
    b.add_argument("--evid", required=True)
    b.add_argument("--mode", default="gpu", choices=MODE_CHOICES)
    b.add_argument("--stage", default=None)
    b.add_argument("--json", default=None, help="also write the budget object here")
    b.add_argument("--quiet", action="store_true", help="no output when the state is ok")
    s = sub.add_parser("snapshot")
    s.add_argument("out")
    st = sub.add_parser("static")
    st.add_argument("out")
    r = sub.add_parser("record")
    r.add_argument("--evid", required=True)
    r.add_argument("--index", type=int, required=True)
    r.add_argument("--stage", required=True)
    r.add_argument("--mode", default="gpu", choices=MODE_CHOICES)
    r.add_argument("--status", required=True, choices=["ok", "failed", "skipped"])
    r.add_argument("--exit-code", type=int, default=0)
    r.add_argument("--started", required=True)
    r.add_argument("--ended", required=True)
    r.add_argument("--log", default=None)
    r.add_argument("--before", default=None)
    r.add_argument("--after", default=None)
    r.add_argument("--notes", default=None)
    w = sub.add_parser("wait-http")
    w.add_argument("url")
    w.add_argument("--timeout", type=float, default=1800.0)
    w.add_argument("--interval", type=float, default=10.0)
    sub.add_parser("free-port")
    sub.add_parser("now")
    dm = sub.add_parser("detect-mode", help="print droplet | notebook | endpoint for this machine (exit 1: none)")
    dm.add_argument("--why", action="store_true", help="one line: the mode, a tab, the reasons")
    ti = sub.add_parser("torch-info", help="torch / ROCm build / GPU name and memory as JSON")
    ti.add_argument("out", nargs="?", default=None)
    sub.add_parser("gpu-mem", help="print '<gpu> <name> <total_mb> <used_mb>' per GPU (nothing without a GPU)")
    t = sub.add_parser("tsv-json", help="preflight helper: TSV (check, status, detail) on stdin -> JSON file")
    t.add_argument("out")
    t.add_argument("--mode", default="gpu")
    args = ap.parse_args(argv)

    if args.cmd == "window":
        state = window_state()
        print(f"event window {EVENT_START} .. {EVENT_END}: now {iso(now())} is {state}")
        return {"before": EXIT_BEFORE, "inside": 0, "after": EXIT_AFTER}[state]
    if args.cmd == "now":
        print(iso(now()))
        return 0
    if args.cmd == "budget":
        info = budget(Path(args.evid), args.mode)
        if args.json:
            _write(args.json, info)
        if not (args.quiet and info["state"] == "ok"):
            print(budget_line(info, args.stage))
        return {"ok": 0, "warn": EXIT_WARN, "stop": EXIT_STOP}[info["state"]]
    if args.cmd == "snapshot":
        _write(args.out, snapshot())
        return 0
    if args.cmd == "static":
        info = static_info()
        _write(args.out, info)
        names = ", ".join(g["name"] + (" (SR-IOV VF)" if g["sriov_vf"] else "") for g in info.get("gpus") or [])
        print(f"GPU: {names or 'none'} | ROCm {info.get('rocm_version') or 'n/a'}")
        return 0
    if args.cmd == "record":
        evid = Path(args.evid)
        info = budget(evid, args.mode)
        out = record(evid, index=args.index, stage=args.stage, mode=args.mode, status=args.status,
                     exit_code=args.exit_code, started=args.started, ended=args.ended, log=args.log,
                     before=args.before, after=args.after, notes=Path(args.notes) if args.notes else None,
                     budget_info=info)
        print(f"[stage] {args.stage}: {args.status} -> {out.relative_to(evid)}")
        return 0
    if args.cmd == "wait-http":
        key = os.environ.get("VLLM_API_KEY") or os.environ.get("WAPSI_LLM_API_KEY") or None
        ok = wait_http(args.url, args.timeout, args.interval, api_key=key)
        print(f"{'up' if ok else 'TIMEOUT'}: {args.url}")
        return 0 if ok else 1
    if args.cmd == "free-port":
        print(free_port())
        return 0
    if args.cmd == "detect-mode":
        mode, why = detect_mode()
        if args.why:
            print(f"{mode}	{'; '.join(why)}")
        else:
            print(mode)
            print("; ".join(why), file=sys.stderr)
        return 0 if mode != "none" else 1
    if args.cmd == "torch-info":
        info = torch_info()
        if args.out:
            _write(args.out, info)
        print(f"torch {info.get('torch') or 'n/a'} | ROCm (HIP) {info.get('hip') or 'n/a'} | GPU "
              f"{info.get('device_name') or 'none'} | {info.get('vram_total_mb') or 'n/a'} MB"
              + (f" | {info['reason']}" if info.get("reason") else ""))
        return 0
    if args.cmd == "gpu-mem":
        snap = tel.sample_once(map_docker=False)
        names = tel.parse_amd_smi_static(tel._json(tel.run_cmd(["amd-smi", "static", "--json"]))) \
            if snap.source == "amd-smi" else {}
        for g in snap.gpus:
            name = names.get(g.gpu, "GPU").replace(" ", "_")
            total = "?" if g.vram_total_mb is None else f"{g.vram_total_mb:.0f}"
            used = "?" if g.vram_used_mb is None else f"{g.vram_used_mb:.0f}"
            print(f"{g.gpu} {name} {total} {used}")
        return 0
    if args.cmd == "tsv-json":
        checks = []
        for line in sys.stdin.read().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                checks.append({"check": parts[0], "status": parts[1], "detail": "\t".join(parts[2:])})
        _write(args.out, {"schema": "wapsi-amd-preflight/1", "mode": canon_mode(args.mode), "checked_at": iso(now()),
                          "fails": sum(c["status"] == "FAIL" for c in checks),
                          "warns": sum(c["status"] == "WARN" for c in checks), "checks": checks})
        return 0
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
