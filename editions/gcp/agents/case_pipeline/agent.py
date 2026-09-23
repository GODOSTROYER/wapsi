"""ADK entry point for the Pub/Sub intake workflow (``POST /apps/case_pipeline/trigger/pubsub``)."""

import _bootstrap  # noqa: F401  (puts the repo root on sys.path)

from editions.gcp.app.workflow import app, root_agent

__all__ = ["app", "root_agent"]
