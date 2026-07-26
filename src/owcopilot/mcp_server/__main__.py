# `python -m owcopilot.mcp_server` 启动入口：等价于 CLI 的 `owcopilot mcp-serve`。
"""``python -m owcopilot.mcp_server`` — start the owcopilot MCP server over a chosen transport.

This mirrors the ``owcopilot mcp-serve`` CLI subcommand so an MCP host that prefers a ``python -m``
launch line (the common convention in host config JSON) can start the server directly.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .serve import run_server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m owcopilot.mcp_server",
        description="Start the owcopilot MCP server (tools + read-only resources + prompt).",
    )
    parser.add_argument(
        "--content-root",
        required=True,
        help="Project content root to serve; opened once and shared for the server's lifetime.",
    )
    parser.add_argument(
        "--sqlite-path",
        default=None,
        help="Runtime DB path (default: <content-root>/.owcopilot/runtime.sqlite).",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio — what an MCP host launches as a subprocess).",
    )
    parser.add_argument("--name", default="owcopilot", help="Server name advertised to clients.")
    args = parser.parse_args(argv)
    run_server(
        args.content_root,
        sqlite_path=args.sqlite_path,
        transport=args.transport,
        name=args.name,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
