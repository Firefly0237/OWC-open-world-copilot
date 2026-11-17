"""Run the local workbench with persistent storage and an empty initial workspace."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def initialize_workspace(name: str, data_dir: Path) -> dict[str, str]:
    from owcopilot.app.workspaces import create_managed_world, sanitize_world_name
    from owcopilot.service.dependencies import _project_content_root
    from owcopilot.util import load_dotenv

    load_dotenv()
    storage = data_dir.expanduser().resolve()
    os.environ["OWCOPILOT_WORLDS_HOME"] = str(storage / "worlds")
    os.environ["OWCOPILOT_RUNTIME_DIR"] = str(storage / "runtime")
    os.environ["OWCOPILOT_PROFILE"] = "local"
    workspace = sanitize_world_name(name)
    content_root = _project_content_root(workspace)
    if content_root is None:
        content_root = create_managed_world(workspace)
    return {
        "workspace": workspace,
        "content_root": str(content_root),
        "worlds_home": os.environ["OWCOPILOT_WORLDS_HOME"],
        "runtime_dir": os.environ["OWCOPILOT_RUNTIME_DIR"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", default="local")
    parser.add_argument(
        "--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "var"
    )
    parser.add_argument(
        "--check", action="store_true", help="Initialize and report paths without serving."
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    try:
        context = initialize_workspace(args.workspace, args.data_dir)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(context, ensure_ascii=False, indent=2), flush=True)
    if args.check:
        return 0

    import uvicorn

    print(f"创作工作台：http://127.0.0.1:{args.port}/creator", flush=True)
    print(f"流程调试台：http://127.0.0.1:{args.port}/evolution", flush=True)
    uvicorn.run(
        "owcopilot.service.api:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
