"""Fail when published Markdown points at a missing repository-local path."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
HTML_LINK = re.compile(r"(?:src|href)=['\"](?P<target>[^'\"]+)['\"]")
EXTERNAL_SCHEMES = {"data", "http", "https", "javascript", "mailto"}


def _published_markdown() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    paths = []
    for relative in result.stdout.splitlines():
        path = REPOSITORY_ROOT / relative
        if path.suffix.lower() == ".md" and path.is_file():
            paths.append(path)
    return sorted(paths)


def _targets(text: str) -> list[str]:
    return [
        match.group("target").strip("<>")
        for pattern in (MARKDOWN_LINK, HTML_LINK)
        for match in pattern.finditer(text)
    ]


def _missing(source: Path, target: str) -> Path | None:
    split = urlsplit(target)
    if split.scheme.lower() in EXTERNAL_SCHEMES or target.startswith("#"):
        return None
    raw_path = unquote(split.path)
    if not raw_path:
        return None
    candidate = (source.parent / raw_path).resolve()
    try:
        candidate.relative_to(REPOSITORY_ROOT)
    except ValueError:
        return candidate
    return None if candidate.exists() else candidate


def main() -> int:
    failures: list[str] = []
    for source in _published_markdown():
        text = source.read_text(encoding="utf-8")
        for target in _targets(text):
            missing = _missing(source, target)
            if missing is not None:
                failures.append(
                    f"{source.relative_to(REPOSITORY_ROOT)} -> {target} (missing: {missing})"
                )
    if failures:
        print("Broken repository-local Markdown links:", file=sys.stderr)
        print("\n".join(f"- {item}" for item in sorted(set(failures))), file=sys.stderr)
        return 1
    print(f"Markdown links OK ({len(_published_markdown())} published files checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
