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
        # Strip null bytes and other problematic characters
        path_str = path_str.replace("\x00", "")
        p = Path(path_str).expanduser().resolve()
        if not p.exists():
            return None
        return p
    except (Exception, ValueError):
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


@router.post("/mkdir")
async def make_directory(request: Request, _: bool = Depends(verify_admin)):
    """Create a new directory. Body: {path}."""
    body = await request.json()
    path_str = str(body.get("path", "")).strip()
    if not path_str:
        return JSONResponse({"error": "path is required"}, status_code=400)
    p = Path(path_str).expanduser().resolve()
    try:
        p.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(p)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/create-file")
async def create_file(request: Request, _: bool = Depends(verify_admin)):
    """Create a new empty file or with content. Body: {path, content?}."""
    body = await request.json()
    path_str = str(body.get("path", "")).strip()
    content = str(body.get("content", ""))
    if not path_str:
        return JSONResponse({"error": "path is required"}, status_code=400)
    p = Path(path_str).expanduser().resolve()
    if p.exists():
        return JSONResponse({"error": "File already exists"}, status_code=409)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(p)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/write-file")
async def write_file(request: Request, _: bool = Depends(verify_admin)):
    """Write content to an existing file. Body: {path, content}."""
    body = await request.json()
    path_str = str(body.get("path", "")).strip()
    content = body.get("content")
    if not path_str:
        return JSONResponse({"error": "path is required"}, status_code=400)
    if content is None:
        return JSONResponse({"error": "content is required"}, status_code=400)
    p = Path(path_str).expanduser().resolve()
    if not p.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    if not p.is_file():
        return JSONResponse({"error": "Not a file"}, status_code=400)
    try:
        p.write_text(str(content), encoding="utf-8")
        return {"ok": True, "path": str(p), "bytes": len(str(content).encode("utf-8"))}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rename")
async def rename_item(request: Request, _: bool = Depends(verify_admin)):
    """Rename a file or directory. Body: {path, new_name}."""
    body = await request.json()
    path_str = str(body.get("path", "")).strip()
    new_name = str(body.get("new_name", "")).strip()
    if not path_str or not new_name:
        return JSONResponse({"error": "path and new_name are required"}, status_code=400)
    src = Path(path_str).expanduser().resolve()
    if not src.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    dst = src.parent / new_name
    if dst.exists():
        return JSONResponse({"error": f"'{new_name}' already exists"}, status_code=409)
    try:
        src.rename(dst)
        return {"ok": True, "old": str(src), "new": str(dst)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/delete")
async def delete_item(request: Request, _: bool = Depends(verify_admin)):
    """Delete a file or directory. Body: {path}."""
    import shutil
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    path_str = str(body.get("path", "")).strip().replace("\x00", "")
    if not path_str:
        return JSONResponse({"error": "path is required"}, status_code=400)
    try:
        p = Path(path_str).expanduser().resolve()
    except Exception as e:
        return JSONResponse({"error": f"Invalid path: {e}"}, status_code=400)
    if not p.exists():
        # Try matching by filename in parent dir (handles weird filenames with backslashes)
        parent = p.parent
        target_name = p.name
        if parent.exists():
            for item in parent.iterdir():
                if item.name == target_name:
                    p = item
                    break
            else:
                return JSONResponse({"error": f"Not found: {path_str}"}, status_code=404)
        else:
            return JSONResponse({"error": f"Not found: {path_str}"}, status_code=404)
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return {"ok": True, "deleted": str(p)}
    except PermissionError:
        return JSONResponse({"error": f"Permission denied: {p.name}"}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": f"Delete failed: {e}"}, status_code=500)


@router.post("/copy")
async def copy_item(request: Request, _: bool = Depends(verify_admin)):
    """Copy a file or directory. Body: {source, destination}."""
    import shutil
    body = await request.json()
    src_str = str(body.get("source", "")).strip()
    dst_str = str(body.get("destination", "")).strip()
    if not src_str or not dst_str:
        return JSONResponse({"error": "source and destination are required"}, status_code=400)
    src = Path(src_str).expanduser().resolve()
    dst = Path(dst_str).expanduser().resolve()
    if not src.exists():
        return JSONResponse({"error": "Source not found"}, status_code=404)
    try:
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return {"ok": True, "source": str(src), "destination": str(dst)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/move")
async def move_item(request: Request, _: bool = Depends(verify_admin)):
    """Move (cut+paste) a file or directory. Body: {source, destination}."""
    import shutil
    body = await request.json()
    src_str = str(body.get("source", "")).strip()
    dst_str = str(body.get("destination", "")).strip()
    if not src_str or not dst_str:
        return JSONResponse({"error": "source and destination are required"}, status_code=400)
    src = Path(src_str).expanduser().resolve()
    dst = Path(dst_str).expanduser().resolve()
    if not src.exists():
        return JSONResponse({"error": "Source not found"}, status_code=404)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return {"ok": True, "source": str(src), "destination": str(dst)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/upload")
async def upload_file(request: Request, path: str = "", _: bool = Depends(verify_admin)):
    """Upload a file via multipart form. Query: ?path=/target/dir."""
    from fastapi import UploadFile, File, Form
    form = await request.form()
    file = form.get("file")
    if not file:
        return JSONResponse({"error": "No file uploaded"}, status_code=400)

    target_dir = Path(path or "~").expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / file.filename

    try:
        content = await file.read()
        dest.write_bytes(content)
        return {"ok": True, "path": str(dest), "size": len(content)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
