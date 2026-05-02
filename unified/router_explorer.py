"""FastAPI router for /api/explorer/* — File explorer endpoints.

Provides browsing, viewing, and downloading files from the VPS filesystem.
Requires admin auth (same as dashboard).
"""

from __future__ import annotations

import mimetypes
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, Response

from .auth_middleware import verify_admin

router = APIRouter(prefix="/api/explorer", tags=["explorer"])

# Root paths that are browsable (home + mcp workspaces)
_HOME = Path.home()


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore
    return f"{size:.1f} TB"


def _safe_resolve(path_str: str) -> Path | None:
    """Resolve a path string. Returns None if invalid."""
    if not path_str:
        return _HOME
    try:
        p = Path(path_str).expanduser().resolve()
        if not p.exists():
            return None
        return p
    except Exception:
        return None


@router.get("/list")
async def list_directory(path: str = "~", _: bool = Depends(verify_admin)):
    """List files and directories at the given path.

    Returns entries with name, type, size, modified time.
    """
    resolved = _safe_resolve(path)
    if resolved is None:
        return JSONResponse({"error": f"Path not found: {path}"}, status_code=404)
    if not resolved.is_dir():
        return JSONResponse({"error": f"Not a directory: {path}"}, status_code=400)

    entries = []
    try:
        for item in sorted(resolved.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                st = item.stat()
                entries.append({
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "size": st.st_size if item.is_file() else 0,
                    "size_human": _human_size(st.st_size) if item.is_file() else "",
                    "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    "extension": item.suffix.lower() if item.is_file() else "",
                })
            except (PermissionError, OSError):
                entries.append({
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "size": 0,
                    "size_human": "",
                    "modified": "",
                    "extension": "",
                    "error": "permission denied",
                })
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)

    return {
        "path": str(resolved),
        "parent": str(resolved.parent) if resolved != resolved.parent else "",
        "entries": entries,
        "count": len(entries),
    }


@router.get("/read")
async def read_file(path: str, _: bool = Depends(verify_admin)):
    """Read a text file and return its content. For preview in the UI.

    Returns up to 500KB of text content. Binary files return an error.
    """
    resolved = _safe_resolve(path)
    if resolved is None:
        return JSONResponse({"error": f"File not found: {path}"}, status_code=404)
    if not resolved.is_file():
        return JSONResponse({"error": f"Not a file: {path}"}, status_code=400)

    # Check size
    size = resolved.stat().st_size
    if size > 512 * 1024:  # 500KB limit for preview
        return {
            "path": str(resolved),
            "size": size,
            "size_human": _human_size(size),
            "too_large": True,
            "message": "File too large for preview. Use download instead.",
        }

    # Detect if binary
    mime, _ = mimetypes.guess_type(str(resolved))
    is_image = mime and mime.startswith("image/")
    is_text = _is_text_file(resolved, mime)

    if is_image:
        return {
            "path": str(resolved),
            "type": "image",
            "mime": mime,
            "size": size,
            "size_human": _human_size(size),
            "url": f"/api/explorer/raw?path={_url_encode(str(resolved))}",
        }

    if not is_text:
        return {
            "path": str(resolved),
            "type": "binary",
            "mime": mime or "application/octet-stream",
            "size": size,
            "size_human": _human_size(size),
            "message": "Binary file. Use download.",
        }

    # Read text content
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
        return {
            "path": str(resolved),
            "type": "text",
            "mime": mime or "text/plain",
            "size": size,
            "size_human": _human_size(size),
            "content": content,
            "extension": resolved.suffix.lower(),
            "name": resolved.name,
        }
    except Exception as e:
        return JSONResponse({"error": f"Read error: {e}"}, status_code=500)


@router.get("/raw")
async def raw_file(path: str, _: bool = Depends(verify_admin)):
    """Serve a file directly (for images, downloads, etc.)."""
    resolved = _safe_resolve(path)
    if resolved is None:
        return JSONResponse({"error": f"File not found: {path}"}, status_code=404)
    if not resolved.is_file():
        return JSONResponse({"error": f"Not a file: {path}"}, status_code=400)

    mime, _ = mimetypes.guess_type(str(resolved))
    return FileResponse(
        str(resolved),
        media_type=mime or "application/octet-stream",
        filename=resolved.name,
    )


@router.get("/download")
async def download_file(path: str, _: bool = Depends(verify_admin)):
    """Download a file with Content-Disposition: attachment."""
    resolved = _safe_resolve(path)
    if resolved is None:
        return JSONResponse({"error": f"File not found: {path}"}, status_code=404)
    if not resolved.is_file():
        return JSONResponse({"error": f"Not a file: {path}"}, status_code=400)

    mime, _ = mimetypes.guess_type(str(resolved))
    return FileResponse(
        str(resolved),
        media_type=mime or "application/octet-stream",
        filename=resolved.name,
        headers={"Content-Disposition": f'attachment; filename="{resolved.name}"'},
    )


def _url_encode(s: str) -> str:
    """URL-encode a string for query params."""
    from urllib.parse import quote
    return quote(s, safe="")


def _is_text_file(path: Path, mime: str | None) -> bool:
    """Heuristic: is this file likely text/code?"""
    # Known text extensions
    text_exts = {
        ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh",
        ".bash", ".zsh", ".fish", ".env", ".gitignore", ".dockerignore",
        ".dockerfile", ".xml", ".svg", ".csv", ".log", ".sql", ".rs", ".go",
        ".java", ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".lua", ".vim",
        ".el", ".clj", ".ex", ".exs", ".erl", ".hs", ".ml", ".r", ".jl",
        ".swift", ".kt", ".scala", ".dart", ".vue", ".svelte", ".astro",
        ".prisma", ".graphql", ".proto", ".tf", ".nix", ".lock",
        ".editorconfig", ".prettierrc", ".eslintrc", ".babelrc",
    }
    if path.suffix.lower() in text_exts:
        return True
    if path.name.lower() in {
        "makefile", "dockerfile", "vagrantfile", "gemfile", "rakefile",
        "procfile", "brewfile", "license", "readme", "changelog",
        "authors", "contributors", "todo", "version",
    }:
        return True
    if mime and (mime.startswith("text/") or mime in ("application/json", "application/xml", "application/javascript")):
        return True
    # Check first bytes for binary content
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
        if b"\x00" in chunk:
            return False
        return True
    except Exception:
        return False
