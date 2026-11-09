"""HTTP server: webhook, health checks and Prometheus metrics."""

from codereview.server.app import create_app

__all__ = ["create_app"]
