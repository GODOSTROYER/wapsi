"""``FileCaseStore``: ``wapsi.ports.CaseStore`` as one JSON file per case (AMD edition, on-prem).

Layout::

    <root>/cases/<case_id>.json      the whole CaseBundle (pydantic JSON)
    <root>/.lock                     advisory lock shared by every process using the store

Writes are atomic: the bundle is written to a temporary file in the same directory, flushed and fsynced, then
moved over the old file with ``os.replace``. Readers therefore see either the previous or the new bundle, never
a partial one. ``append_audit`` is a read-modify-write under the store lock (a ``threading.RLock`` inside the
process plus ``fcntl.flock`` / ``msvcrt.locking`` across processes, e.g. the API and the Streamlit UI in the
same container). Case ids are restricted to ``[A-Za-z0-9._-]`` so they can never escape the directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

from wapsi.models import AuditEvent, Case, CaseBundle
from wapsi.timeutil import ensure_utc

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _check_id(case_id: str) -> str:
    if not isinstance(case_id, str) or not _SAFE_ID.match(case_id) or ".." in case_id:
        raise ValueError(f"unsafe case id {case_id!r}")
    return case_id


try:  # POSIX (the container)
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows (local development)
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]


@contextlib.contextmanager
def _os_lock(path: Path) -> Iterator[None]:
    """Advisory cross-process lock on ``path`` (best effort where the platform has no locking)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - neither available
            yield


class FileCaseStore:
    """Implements ``wapsi.ports.CaseStore``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.cases_dir = self.root / "cases"
        self.cases_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".lock"
        self._mutex = threading.RLock()

    def __repr__(self) -> str:
        return f"FileCaseStore({str(self.root)!r})"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._mutex, _os_lock(self._lock_path):
            yield

    def _path(self, case_id: str) -> Path:
        return self.cases_dir / f"{_check_id(case_id)}.json"

    def _write(self, path: Path, data: str) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=self.cases_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _read(self, path: Path) -> CaseBundle | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return CaseBundle.model_validate_json(raw)

    # -- CaseStore protocol
    def put_bundle(self, bundle: CaseBundle) -> None:
        path = self._path(bundle.case.case_id)
        data = bundle.model_dump_json()
        with self._locked():
            self._write(path, data)

    def get_bundle(self, case_id: str) -> CaseBundle | None:
        return self._read(self._path(case_id))

    def list_cases(self) -> list[Case]:
        out: list[Case] = []
        for p in sorted(self.cases_dir.glob("*.json")):
            if p.name.startswith("."):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except FileNotFoundError:  # replaced or deleted between glob and read
                continue
            out.append(Case.model_validate(data["case"]))
        return sorted(out, key=lambda c: ensure_utc(c.opened_at), reverse=True)

    def append_audit(self, case_id: str, event: AuditEvent) -> None:
        path = self._path(case_id)
        with self._locked():
            bundle = self._read(path)
            if bundle is None:
                raise KeyError(case_id)
            bundle.audit.append(event)
            self._write(path, bundle.model_dump_json())

    # -- convenience (not part of the protocol)
    def case_ids(self) -> list[str]:
        return [c.case_id for c in self.list_cases()]
