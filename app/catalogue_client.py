"""How the pipeline reaches the catalogue: over MCP, like any other client would.

`McpCatalogue` starts the catalogue MCP server (app/mcp_server.py) as a subprocess over
stdio and keeps it running. It serves two callers:
  * code: `search()` pre-fetches the top candidates for every request line;
  * the matching agent: `agent_tools()` is the same server's read-only tools as CrewAI
    tools, so the agent can run its own searches (a different wording, a category filter).

`LocalCatalogue` gives the same `search()` in-process, for unit tests and the API's
catalogue endpoint, where a subprocess would only add start-up time.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Protocol

from app.catalogue.index import Catalogue, get_catalogue
from app.config import PROJECT_ROOT
from app.schema import Candidate

AGENT_TOOLS = ("search_products", "get_product", "check_stock")


class CatalogueSearch(Protocol):
    def search(self, query: str, k: int = 5) -> list[Candidate]: ...

    def agent_tools(self) -> list: ...


class LocalCatalogue:
    def __init__(self, catalogue: Catalogue | None = None) -> None:
        self.catalogue = catalogue or get_catalogue()

    def search(self, query: str, k: int = 5) -> list[Candidate]:
        return [Candidate(**h.as_dict()) for h in self.catalogue.search(query, k=k)]

    def agent_tools(self) -> list:
        return []


class McpCatalogue:
    def __init__(self, connect_timeout: int = 60) -> None:
        self._timeout = connect_timeout
        self._adapter = None
        self._tools: dict = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        from crewai_tools import MCPServerAdapter
        from mcp import StdioServerParameters

        with self._lock:
            if self._adapter is not None:
                return
            params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"],
                                           cwd=str(PROJECT_ROOT), env=dict(os.environ))
            self._adapter = MCPServerAdapter(params, connect_timeout=self._timeout)
            self._tools = {t.name: t for t in self._adapter.tools}

    def stop(self) -> None:
        with self._lock:
            if self._adapter is not None:
                self._adapter.stop()
                self._adapter = None
                self._tools = {}

    def call(self, tool: str, **kwargs: object) -> dict:
        self.start()
        raw = self._tools[tool].run(**kwargs)
        return json.loads(raw) if isinstance(raw, str) else raw

    def search(self, query: str, k: int = 5) -> list[Candidate]:
        result = self.call("search_products", query=query, k=k)
        if "error" in result:
            raise RuntimeError(result["error"])
        return [Candidate(**r) for r in result["results"]]

    def agent_tools(self) -> list:
        self.start()
        return [self._tools[name] for name in AGENT_TOOLS]
