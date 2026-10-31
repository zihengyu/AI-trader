"""Application-local file isolation for read-only research tools."""
from urllib.parse import urlsplit

from lumibot.components.agents.browser_tools import BrowserSessionManager

from .risk import RiskError


def web_url(url):
    if url is not None and urlsplit(str(url)).scheme.lower() not in {"http", "https"}:
        raise RiskError("Research browser navigation requires an HTTP(S) URL")


class ResearchBrowser(BrowserSessionManager):
    """Preserve web research without exposing local configuration or credentials."""
    def navigate(self, session_id, url, **kwargs):
        web_url(url)
        return super().navigate(session_id, url, **kwargs)

    def tabs(self, session_id, operation, **kwargs):
        web_url(kwargs.get("url"))
        return super().tabs(session_id, operation, **kwargs)


def isolate_research(strategy, root):
    # Native DuckDB's prefix check accepts multiple statements. Engine-level
    # permissions prevent COPY/read_text/ATTACH/extension access to the risk
    # journal and secrets, even after a SELECT. Registered tables still work.
    connection = strategy.agents.duckdb.connection
    connection.execute("SET enable_external_access=false")
    connection.execute("SET lock_configuration=true")
    strategy._agent_browser_manager = ResearchBrowser(
        state_root=root / "browser", upload_root=root / "browser" / "uploads")
