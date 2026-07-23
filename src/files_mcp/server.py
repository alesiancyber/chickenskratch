"""Read-only filesystem search MCP server.

The find/grep you never have to remember flags for — as agent tools.
Strictly read-only (every tool carries readOnlyHint, so the harness
never gates them). Search is confined to the FILES_ROOTS allowlist
(colon-separated, default "~"); anything resolving outside is rejected.
Traversal skips hidden dirs, node_modules/__pycache__, and macOS bulk
dirs, and every walk runs under a time budget so a huge tree returns
partial results instead of hanging.

Run: FILES_ROOTS="~" files-mcp
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True)
mcp = FastMCP("files")

SKIP_NAMES = {"node_modules", "__pycache__", "Library", "Applications", "Movies", "Music", "Pictures"}
SKIP_SUFFIXES = (".photoslibrary", ".app", ".framework", ".bundle")
MAX_FILE_BYTES = 2_000_000
WALK_DEADLINE_S = 8.0


def _roots() -> list[Path]:
    raw = os.environ.get("FILES_ROOTS", "~")
    roots = [p for part in raw.split(":") if (p := Path(part).expanduser().resolve()).is_dir()]
    if not roots:
        raise ValueError(f"No valid directories in FILES_ROOTS={raw!r}")
    return roots


def _resolve_scope(root_arg: str) -> list[Path]:
    """Narrow the search to one dir inside the allowlist, or all roots."""
    roots = _roots()
    if not root_arg.strip():
        return roots
    p = Path(root_arg.strip()).expanduser()
    candidates = [p] if p.is_absolute() else [r / p for r in roots]
    # Traversal prunes hidden dirs, so an explicit root must not re-open
    # them: root="~/.ssh" under FILES_ROOTS="~" would read private keys.
    # A dir the operator allowlisted directly (hidden parts at or above
    # some root `r`) stays reachable through that root.
    hidden = False
    for c in candidates:
        rc = c.resolve()
        if not rc.is_dir():
            continue
        for r in roots:
            if rc.is_relative_to(r):
                if any(part.startswith(".") for part in rc.relative_to(r).parts):
                    hidden = True
                else:
                    return [rc]
    if hidden:
        raise ValueError(f"root {root_arg!r} is inside a hidden directory, which search never enters")
    raise ValueError(f"root must be an existing directory inside {[str(r) for r in roots]}")


def _walk(scopes: list[Path], deadline: float):
    """Yield (dirpath, dirnames, filenames); yields None once if the time
    budget runs out. dirnames is post-pruning, so matches on it are dirs
    that would actually be descended into."""
    for scope in scopes:
        for dirpath, dirnames, filenames in os.walk(scope):
            if time.monotonic() > deadline:
                yield None
                return
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in SKIP_NAMES and not d.endswith(SKIP_SUFFIXES)
            ]
            yield Path(dirpath), dirnames, filenames


def _display(p: Path) -> str:
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


def _is_texty(p: Path) -> bool:
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return False
        with open(p, "rb") as f:
            return b"\0" not in f.read(8192)
    except OSError:
        return False


@mcp.tool(annotations=READ_ONLY)
def find_file(name: str, root: str = "", limit: int = 20) -> dict:
    """Find files AND directories by name (case-insensitive, partial names
    fine) and return their paths — directories carry a trailing "/".
    Exact-name matches rank first. Optional `root` narrows the search.

    Example: find_file(name="rust-whois") ->
      {"matches": ["~/Desktop/rust-whois/"], "truncated": false}
    """
    needle = name.lower().strip()
    if not needle:
        raise ValueError("name is required")
    deadline = time.monotonic() + WALK_DEADLINE_S
    exact: list[Path] = []
    partial: list[Path] = []
    truncated = False
    for item in _walk(_resolve_scope(root), deadline):
        if item is None:
            truncated = True
            break
        dirpath, dirnames, filenames = item
        for entry in dirnames + filenames:
            low = entry.lower()
            if low == needle or low.rsplit(".", 1)[0] == needle:
                exact.append(dirpath / entry)
            elif needle in low:
                partial.append(dirpath / entry)
        if len(exact) >= limit:
            break
    matches = (exact + partial)[:limit]
    result = {
        "query": name,
        "matches": [_display(p) + ("/" if p.is_dir() else "") for p in matches],
        "truncated": truncated,
    }
    if truncated:
        result["note"] = "search hit its time budget — pass a narrower `root` for full coverage"
    return result


@mcp.tool(annotations=READ_ONLY)
def search_text(text: str, root: str = "", limit: int = 30) -> dict:
    """Find text inside files (case-insensitive substring), returning
    path, line number, and the matching line. Skips binaries and files over
    2MB. Prefer a `root` directory when you know roughly where to look —
    unscoped searches over a large tree may return partial results.

    Example: search_text(text="confirm_writes", root="~/Desktop/tool-harness") ->
      {"matches": [{"file": ".../models.yaml", "line": 12, "text": "confirm_writes: true"}]}
    """
    needle = text.lower()
    if not needle.strip():
        raise ValueError("text is required")
    deadline = time.monotonic() + WALK_DEADLINE_S
    matches: list[dict] = []
    truncated = False
    for item in _walk(_resolve_scope(root), deadline):
        if item is None:
            truncated = True
            break
        dirpath, _dirnames, filenames = item
        for fn in filenames:
            # Re-check per file: one directory of large files could otherwise
            # blow far past the budget between _walk's per-directory checks.
            if time.monotonic() > deadline:
                truncated = True
                break
            p = dirpath / fn
            if not _is_texty(p):
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(content.splitlines(), 1):
                if needle in line.lower():
                    matches.append({"file": _display(p), "line": i, "text": line.strip()[:200]})
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break
        if truncated or len(matches) >= limit:
            break
    result = {"query": text, "matches": matches, "truncated": truncated or len(matches) >= limit}
    if result["truncated"]:
        result["note"] = "partial results — narrow with `root` or a more specific search"
    return result


@mcp.tool(annotations=READ_ONLY)
def list_dir(path: str = "") -> dict:
    """List one directory (non-recursive): subdirectories and files with
    sizes. Defaults to the first allowed root.

    Example: list_dir(path="~/Desktop/tool-harness") ->
      {"path": "...", "dirs": ["config", "src"], "files": [{"name": "README.md", "size": 4321}]}
    """
    scope = _resolve_scope(path)[0]
    dirs, files = [], []
    for entry in sorted(scope.iterdir(), key=lambda e: e.name.lower()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            dirs.append(entry.name)
        else:
            try:
                files.append({"name": entry.name, "size": entry.stat().st_size})
            except OSError:
                continue
    return {"path": _display(scope), "dirs": dirs, "files": files}


def main() -> None:
    _roots()  # fail fast on a bad FILES_ROOTS
    mcp.run()


if __name__ == "__main__":
    main()
