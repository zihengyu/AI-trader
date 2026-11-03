"""Application-local file and network boundaries for read-only research tools."""
import ipaddress
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from lumibot.components.agents.browser_tools import BrowserSessionManager

from .risk import RiskError

# RFC 2544 benchmarking range; Clash/OpenClash "fake-ip" DNS answers live here.
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
# The agents' DuckDB runs SQL the model writes. 2026-11-03: one such query (trailing returns and
# volatility over the universe) never finished and kept both cores of the server busy for 12 hours,
# because DuckDB has no statement timeout and a Python timeout cannot stop native code.
DUCKDB_QUERY_SECONDS = 60
DUCKDB_THREADS = 1  # Never more than one core, whatever the query.
DUCKDB_MEMORY_LIMIT = "1GB"
RESEARCH_HOSTS = ("www.sec.gov", "efts.sec.gov", "finance.yahoo.com", "www.reuters.com", "www.federalreserve.gov")


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


@dataclass(frozen=True)
class ResearchNetwork:
    """How the native HTTP research tool resolves hostnames.

    `system` uses the operating system resolver. `doh` asks a DNS-over-HTTPS
    resolver for public answers, which fixes proxy setups whose local DNS
    returns fake private addresses (Clash fake-ip). Either way the native
    SSRF protection still rejects private, loopback and reserved answers.
    """
    resolver: str = "system"
    doh_url: str = "https://1.1.1.1/dns-query"
    timeout_seconds: int = 5

    def __post_init__(self):
        if self.resolver not in {"system", "doh"}:
            raise RiskError("research_network.resolver must be system or doh")
        if urlsplit(self.doh_url).scheme != "https" or not urlsplit(self.doh_url).hostname:
            raise RiskError("research_network.doh_url must be an https URL")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 30:
            raise RiskError("research_network.timeout_seconds must be 1..30")

    @classmethod
    def from_config(cls, section):
        section = dict(section or {})
        unknown = set(section) - set(cls.__dataclass_fields__)
        if unknown:
            raise RiskError("Unknown research_network settings: " + ", ".join(sorted(unknown)))
        return cls(**section)


class DohResolver:
    """Resolve A/AAAA records over HTTPS (RFC 8484 JSON form); small TTL cache."""

    def __init__(self, url, timeout=5, get=None, clock=time.monotonic):
        self.url, self.timeout, self.get, self.clock = url, timeout, get or requests.get, clock
        self._cache = {}

    def __call__(self, hostname):
        cached = self._cache.get(hostname)
        if cached and cached[0] > self.clock():
            return list(cached[1])
        addresses, ttl = [], 300
        for record_type, code in (("A", 1), ("AAAA", 28)):
            response = self.get(self.url, params={"name": hostname, "type": record_type},
                                headers={"accept": "application/dns-json"}, timeout=self.timeout)
            response.raise_for_status()
            for answer in response.json().get("Answer") or []:
                if answer.get("type") == code:
                    addresses.append(str(ipaddress.ip_address(answer["data"])))
                    ttl = min(ttl, max(30, int(answer.get("TTL", 300))))
        result = sorted(set(addresses))
        if result:
            self._cache[hostname] = (self.clock() + ttl, result)
        return result


def diagnose(resolve, hosts=RESEARCH_HOSTS):
    """Classify resolver answers for research hosts without fetching any page."""
    rows = {}
    for host in hosts:
        try:
            addresses = resolve(host)
        except Exception as exc:
            rows[host] = {"status": "unresolved", "error": type(exc).__name__}
            continue
        ips = [ipaddress.ip_address(a) for a in addresses]
        if not ips:
            status = "unresolved"
        elif any(ip in FAKE_IP_NETWORK for ip in ips):
            status = "fake_ip_dns"
        elif any(ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local for ip in ips):
            status = "private_answer"
        else:
            status = "public"
        rows[host] = {"status": status, "addresses": [str(ip) for ip in ips]}
    blocked = [h for h, r in rows.items() if r["status"] != "public"]
    advice = None
    if any(r["status"] == "fake_ip_dns" for r in rows.values()):
        advice = ("Local DNS returns Clash fake-ip addresses, which the research HTTP tool correctly refuses. "
                  "Set research_network.resolver to \"doh\", or exclude this host from fake-ip mode.")
    elif blocked:
        advice = "Some research hosts did not resolve to public addresses; check DNS and proxy settings."
    return {"hosts": rows, "research_http_usable": not blocked, "advice": advice}


def limit_queries(layer, seconds=DUCKDB_QUERY_SECONDS):
    """Stop an agent's DuckDB query that runs longer than `seconds`.

    A timer interrupts the connection (DuckDB's own cancellation, which stops the
    native work); the agent gets an error telling it to simplify the query. Applied
    to this strategy's query layer only, so LumiBot itself is unchanged.
    """
    original = getattr(layer, "query", None)
    if original is None:
        return layer  # A bare connection (tests, tools without the query layer): settings still apply.

    def query(*, sql, limit=200):
        fired = threading.Event()

        def stop():
            fired.set()
            layer.connection.interrupt()
        timer = threading.Timer(seconds, stop)
        timer.daemon = True
        timer.start()
        try:
            return original(sql=sql, limit=limit)
        except Exception as exc:
            if fired.is_set():
                raise TimeoutError(f"DuckDB query stopped after {seconds} s; use fewer rows, avoid cross joins and "
                                   "compute per symbol or over a shorter window") from exc
            raise
        finally:
            timer.cancel()
    layer.query = query
    return layer


def isolate_research(strategy, root, network=None):
    # Native DuckDB's prefix check accepts multiple statements. Engine-level
    # permissions prevent COPY/read_text/ATTACH/extension access to the risk
    # journal and secrets, even after a SELECT. Registered tables still work,
    # and files the research tools downloaded remain readable for analysis.
    root = Path(root)
    folders = [root / "browser", root / "research"]
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
    connection = strategy.agents.duckdb.connection
    allowed = ", ".join("'" + str(folder.resolve()).replace("'", "''") + "'" for folder in folders)
    connection.execute(f"SET allowed_directories=[{allowed}]")
    connection.execute("SET enable_external_access=false")
    connection.execute(f"SET threads={DUCKDB_THREADS}")
    connection.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    connection.execute("SET lock_configuration=true")  # The model's SQL cannot change any of the above.
    limit_queries(strategy.agents.duckdb)
    strategy._agent_browser_manager = ResearchBrowser(
        state_root=root / "browser", upload_root=root / "browser" / "uploads")
    settings = ResearchNetwork.from_config(network)
    if settings.resolver == "doh":
        from lumibot.components.agents.web_tools import WebClient
        # Same native client and SSRF checks; only the name resolution differs.
        strategy._agent_web_client = WebClient(resolver=DohResolver(settings.doh_url, settings.timeout_seconds))
    return settings
