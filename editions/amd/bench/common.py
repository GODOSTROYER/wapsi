"""Shared helpers for the AMD benchmarks (standard library only)."""

from __future__ import annotations

import json
import math
import platform
import socket
import subprocess
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
RESULTS_ROOT = Path(__file__).resolve().parent / "results"
SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_stamp(dt: datetime | None = None) -> str:
    return (dt or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def percentile(values: Iterable[float], p: float) -> float | None:
    """Linear-interpolated percentile (numpy's default method); None for no values."""
    s = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not s:
        return None
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def dist(values: Iterable[float], digits: int = 4) -> dict[str, float | int | None]:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]

    def r(x: float | None) -> float | None:
        return None if x is None else round(x, digits)

    return {
        "n": len(vals),
        "mean": r(sum(vals) / len(vals)) if vals else None,
        "p50": r(percentile(vals, 50)),
        "p95": r(percentile(vals, 95)),
        "p99": r(percentile(vals, 99)),
        "min": r(min(vals)) if vals else None,
        "max": r(max(vals)) if vals else None,
    }


def git_commit(root: Path = ROOT) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                             timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def env_info() -> dict[str, Any]:
    return {"host": socket.gethostname(), "platform": platform.platform(), "python": platform.python_version(),
            "git_commit": git_commit()}


def results_dir(out: str | Path | None = None, stamp: str | None = None) -> Path:
    path = Path(out) if out else RESULTS_ROOT / (stamp or utc_stamp())
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, obj: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    return path


def fmt(v: Any, digits: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        if v != 0 and abs(v) < 10 ** (-digits):
            return f"{v:.2e}"
        return f"{v:,.{digits}f}"
    return str(v)


def md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]], digits: int = 3) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(fmt(c, digits).replace("|", "\\|") for c in row) + " |")
    return "\n".join(lines)
