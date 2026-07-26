# MCP serve 入口：把工厂产出的 server 真正跑起来，并在 serve 生命周期内绑定一个共享 project。
"""Run the MCP server and bind one shared :class:`ProjectContext` for its whole lifetime.

Why this module exists (C-5 lifespan fix)
------------------------------------------
``mcp_server/tools.py`` shares an already-open :class:`ProjectContext` across many tool calls via
the ``_shared_project`` ContextVar, so one MCP session parses/graphs/indexes the project **once**
instead of once per tool call. Before this module that var was set **only by the CLI agent
commands** — starting a real MCP transport left it unset, so every incoming tool call rebuilt the
whole project context (parse + graph + vector reindex). That is the ``lifespan 缺失`` half of C-5.

This module closes it by opening the project inside a **FastMCP lifespan**: the async context
manager runs once at server startup, sets ``_shared_project`` for the served project, and resets +
closes it at shutdown. FastMCP enters the lifespan in the same task that later dispatches every
request, and ``anyio``'s ``start_soon`` copies that task's context into each request task, so the
ContextVar reliably propagates to every tool handler (verified end-to-end by
``tests/test_mcp_sdk_smoke.py``).

Concurrency safety (the ``并发性能事故`` half of C-5)
----------------------------------------------------
The shared context (one ``sqlite3`` connection + in-memory graph/vector state) is **not**
thread-safe or reentrant, so sharing it is only sound if tool handlers never execute concurrently.
They do not: every owcopilot MCP tool handler is a **synchronous** function, and FastMCP invokes a
sync tool by calling it directly on the event-loop thread (``func_metadata`` does ``return
fn(...)`` — no ``anyio.to_thread`` offload). A synchronous call has no ``await`` points, so it runs
to completion before any other request task can resume: the event loop serialises handler bodies
even though the low-level server ``start_soon``s one task per request. One project, one connection,
one handler at a time — safe.

Documented boundaries (see the module-level report / A5):
  * **Single project per server.** The served project is fixed at launch; a tool's ``content_root``
    argument is accepted for MCP-schema compatibility but ignored while a project is bound (the same
    semantics the CLI agent session already relies on). Serving several projects from one process
    would need per-request project resolution and a per-project lock — deliberately out of scope.
  * **The serialisation guarantee is tied to handlers staying synchronous.** If a handler were ever
    rewritten as ``async`` (introducing ``await`` points), two requests could interleave on the one
    shared connection. Keep MCP tool handlers sync, or add a lock before that changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from ..pipeline.project import ProjectContext
from . import tools
from .transport import create_mcp_server

TransportName = Literal["stdio", "sse", "streamable-http"]


def shared_project_lifespan(
    content_root: str | Path,
    sqlite_path: str | Path | None = None,
) -> Callable[[Any], AbstractAsyncContextManager[dict[str, Any]]]:
    """Build a FastMCP lifespan that binds one open :class:`ProjectContext` for the server's life.

    The returned callable matches FastMCP's ``lifespan`` contract (``lifespan(app) -> async
    context manager``). On enter it opens the project and publishes it via ``tools._shared_project``
    so every tool handler reuses it; on exit it resets the ContextVar and closes the project. The
    yielded dict is FastMCP's lifespan context (available to handlers) and records the bound root.
    """
    root = Path(content_root)
    runtime_path = Path(sqlite_path) if sqlite_path else root / ".owcopilot" / "runtime.sqlite"

    @asynccontextmanager
    async def _lifespan(_app: Any) -> AsyncIterator[dict[str, Any]]:
        if not root.exists():
            raise FileNotFoundError(f"content root does not exist: {root}")
        if str(runtime_path) != ":memory:":
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
        project = ProjectContext.open(root, sqlite_path=runtime_path)
        token = tools._shared_project.set(project)
        try:
            yield {"content_root": str(root), "sqlite_path": str(runtime_path)}
        finally:
            tools._shared_project.reset(token)
            project.close()

    return _lifespan


def build_serving_server(
    content_root: str | Path,
    sqlite_path: str | Path | None = None,
    name: str = "owcopilot",
) -> Any:
    """Create a FastMCP server wired to a lifespan binding ``content_root`` as the shared project.

    Split out from :func:`run_server` so a test (or an alternative host) can construct the exact
    server that ``run_server`` would run — same tools, resources, prompts and lifespan — without
    committing to a blocking transport.
    """
    return create_mcp_server(name=name, lifespan=shared_project_lifespan(content_root, sqlite_path))


def run_server(
    content_root: str | Path,
    *,
    sqlite_path: str | Path | None = None,
    transport: TransportName = "stdio",
    name: str = "owcopilot",
) -> None:
    """Run the MCP server on ``transport`` with the project bound (blocks until the client exits).

    ``stdio`` is the default and the only transport that needs no network binding — it is what an
    MCP host (Claude Desktop, an IDE, an agent runner) launches as a subprocess. ``sse`` /
    ``streamable-http`` are HTTP transports; they honour FastMCP's ``FASTMCP_HOST`` /
    ``FASTMCP_PORT`` settings. The content-root existence check happens inside the lifespan so
    every transport reports the same guided error.
    """
    server = build_serving_server(content_root, sqlite_path=sqlite_path, name=name)
    server.run(transport=transport)
