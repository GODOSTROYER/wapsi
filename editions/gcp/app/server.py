"""FastAPI entry point for Cloud Run: the ADK API (plus the ADK dev UI in demo mode), the Pub/Sub trigger route
for ``case_pipeline``, ``/healthz``, and the role middleware. The Streamlit UI is a separate Cloud Run service.

Run locally (demo mode, no cloud access)::

    uv run uvicorn editions.gcp.app.server:app --port 8080

Identity: on Cloud Run behind IAP the ``X-Goog-Authenticated-User-Email`` header names the user; the path
``user_id`` must match it and the role comes from ``WAPSI_SUPERVISORS`` / ``WAPSI_AUDITORS``. In demo mode the
``X-Wapsi-Role`` header (or a user id starting with ``supervisor``) selects the role; ``WAPSI_TRUST_ROLE_HEADER=1``
allows the same for a public judging deployment on synthetic data. Whatever the client sends,
``user:wapsi_*`` state keys are stripped and a server-signed grant is written instead (see ``identity``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from .config import FIRESTORE_URI, GcpSettings, load_settings
from .identity import ROLES, granted_state, role_for_email, strip_protected
from .memory import register_firestore_services
from .services import build_services, get_services, set_services

log = logging.getLogger("wapsi.gcp.server")

AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"
IAP_EMAIL = b"x-goog-authenticated-user-email"
DEMO_ROLE = b"x-wapsi-role"
_SESSIONS = re.compile(r"^/apps/(?P<app>[^/]+)/users/(?P<user>[^/]+)/sessions(?:/(?P<sid>[^/]+))?/?$")
_USER_SCOPED = re.compile(r"^/apps/[^/]+/users/(?P<user>[^/]+)(?:/|$)")
_RUN = re.compile(r"^/run(?:_sse)?/?$")
MAX_BODY = 5 * 1024 * 1024


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


def resolve_role(scope: dict[str, Any], user_id: str, settings: GcpSettings) -> tuple[str | None, str]:
    """(error, role). ``error`` is set when the request must be rejected."""
    email = _header(scope, IAP_EMAIL)
    if email:
        email = email.split(":", 1)[-1].strip().lower()
        if user_id.lower() != email:
            return "user_id must match the signed-in user", "analyst"
        return None, role_for_email(email, settings.supervisors, settings.auditors)
    if settings.demo_mode or settings.trust_role_header:
        role = (_header(scope, DEMO_ROLE) or "").strip().lower()
        if role in ROLES:
            return None, role
        if user_id.lower().startswith("supervisor"):
            return None, "supervisor"
        default = os.environ.get("WAPSI_DEMO_DEFAULT_ROLE", "analyst").strip().lower()
        return None, default if default in ROLES else "analyst"
    return None, "analyst"


def _scrub_events(events: Any) -> Any:
    if not isinstance(events, list):
        return events
    for ev in events:
        actions = ev.get("actions") if isinstance(ev, dict) else None
        if isinstance(actions, dict):
            for key in ("state_delta", "stateDelta"):
                if isinstance(actions.get(key), dict):
                    actions[key] = strip_protected(actions[key])
    return events


class RoleStateMiddleware:
    """Pure ASGI middleware that owns the identity keys in ADK session state."""

    def __init__(self, app: Any, settings: GcpSettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        scoped = _USER_SCOPED.match(path)
        if scoped and scope.get("method") not in {"POST", "PATCH", "PUT"}:
            # reads and deletes: with IAP, a user only reaches their own sessions, memory and artifacts
            error, _ = resolve_role(scope, scoped.group("user"), self.settings)
            if error:
                await _json_response(send, 403, {"detail": error})
                return
            await self.app(scope, receive, send)
            return
        if scope.get("method") not in {"POST", "PATCH", "PUT"}:
            await self.app(scope, receive, send)
            return
        m = _SESSIONS.match(path)
        if not m and not _RUN.match(path):
            if scoped:
                error, _ = resolve_role(scope, scoped.group("user"), self.settings)
                if error:
                    await _json_response(send, 403, {"detail": error})
                    return
            await self.app(scope, receive, send)
            return

        chunks, size = [], 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            size += len(body)
            if size > MAX_BODY:
                await _json_response(send, 413, {"detail": "request body too large"})
                return
            chunks.append(body)
            more = message.get("more_body", False)
        raw = b"".join(chunks)

        data: Any = None
        if raw.strip():
            try:
                data = json.loads(raw)
            except ValueError:
                await _json_response(send, 400, {"detail": "request body is not valid JSON"})
                return
        body = data if isinstance(data, dict) else {}
        user_id = str(m.group("user") if m else body.get("user_id") or "")
        error, role = resolve_role(scope, user_id, self.settings)
        if error:
            await _json_response(send, 403, {"detail": error})
            return

        if m and scope["method"] == "POST":
            grant = granted_state(user_id, role)
            if m.group("sid") is None:  # create_session: {"session_id", "state", "events"}
                state = body.get("state") if isinstance(body.get("state"), dict) else {}
                body["state"] = {**strip_protected(state), **grant}
                if "events" in body:
                    body["events"] = _scrub_events(body["events"])
            else:  # deprecated create_session_with_id: the body *is* the state dict
                body = {**strip_protected(body), **grant}
            raw = json.dumps(body).encode("utf-8")
        elif isinstance(data, dict):  # PATCH session / run / run_sse carry ``state_delta``
            if isinstance(data.get("state_delta"), dict):
                data["state_delta"] = strip_protected(data["state_delta"])
            raw = json.dumps(data).encode("utf-8")

        headers = [(k, v) for k, v in scope.get("headers") or [] if k.lower() not in {b"content-length",
                                                                                      b"content-type"}]
        headers += [(b"content-length", str(len(raw)).encode()), (b"content-type", b"application/json")]
        scope = {**scope, "headers": headers}
        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _json_response(send: Any, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    root.setLevel(os.environ.get("WAPSI_LOG_LEVEL", "INFO"))


def create_app(settings: GcpSettings | None = None, *, services: Any | None = None) -> Any:
    from fastapi import FastAPI, Header
    from google.adk.cli.fast_api import get_fast_api_app

    settings = settings or load_settings()
    _configure_logging()
    set_services(services or build_services(settings))
    if FIRESTORE_URI in (settings.session_service_uri(), settings.memory_service_uri()):
        register_firestore_services(settings)

    adk_app: FastAPI = get_fast_api_app(
        agents_dir=str(AGENTS_DIR),
        web=settings.serve_adk_web,
        session_service_uri=settings.session_service_uri() or "memory://",
        artifact_service_uri=os.environ.get("WAPSI_ARTIFACT_BUCKET") or "memory://",
        memory_service_uri=settings.memory_service_uri(),
        use_local_storage=False,
        allow_origins=list(settings.allow_origins) or None,
        trigger_sources=["pubsub"],
        trigger_oidc_audience=os.environ.get("WAPSI_TRIGGER_AUDIENCE") or None,
        trigger_oidc_service_accounts=[s for s in os.environ.get("WAPSI_TRIGGER_SERVICE_ACCOUNTS", "").split(",")
                                       if s.strip()] or None,
        otel_to_cloud=settings.otel_enabled(),
    )

    summary = settings.public_summary()

    @adk_app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        svc = get_services()
        guard = svc.guard
        return {
            "status": "ok",
            "service": "wapsi-gcp",
            "settings": summary,
            "guard_fallbacks": len(getattr(guard, "fallback_events", []) or []),
            "docs_index": svc.search is not None,
        }

    @adk_app.get("/wapsi/traces", include_in_schema=False)
    async def agent_traces(case_id: str | None = None, limit: int = 20,
                           x_goog_authenticated_user_email: str | None = Header(default=None)) -> dict[str, Any]:
        """Agent runs this instance recorded (redacted ``AgentTrace``s, oldest first); ``case_id`` filters.
        The same traces are sealed into each case's audit log, which the UI reads. Behind IAP an analyst sees
        only their own runs (as with ADK sessions); supervisors and auditors see every run."""
        from .tracing import get_recorder

        items = get_recorder().traces(case_id=case_id)
        email = x_goog_authenticated_user_email  # set by IAP (it strips any client-sent copy)
        if email:
            email = email.split(":", 1)[-1].strip().lower()
            if role_for_email(email, settings.supervisors, settings.auditors) == "analyst":
                items = [t for t in items if t.user_id.strip().lower() == email]
        items = items[-max(1, min(int(limit), 100)):]
        return {"traces": [t.model_dump(mode="json") for t in items],
                "summaries": [{"trace_id": t.trace_id, "cases": t.case_ids, **t.summary().model_dump(mode="json")}
                              for t in items]}

    adk_app.add_middleware(RoleStateMiddleware, settings=settings)
    return adk_app


def __getattr__(name: str) -> Any:  # lazy ``app`` so importing this module has no side effects
    if name == "app":
        value = create_app()
        globals()["app"] = value
        return value
    raise AttributeError(name)
