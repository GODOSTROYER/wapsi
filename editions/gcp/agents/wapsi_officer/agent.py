"""ADK entry point: ``adk web editions/gcp/agents`` / ``adk api_server editions/gcp/agents``."""

import _bootstrap  # noqa: F401  (puts the repo root on sys.path)

from editions.gcp.app.agent import app, root_agent

__all__ = ["app", "root_agent"]
