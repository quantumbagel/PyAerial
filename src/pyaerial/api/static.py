"""SPA static-file helpers for the web portal."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
FRONTEND_HINT = (
    "Frontend not built. Run: scripts/build_web.sh (or: cd web && npm install && npm run build)"
)


def safe_static_path(full_path: str) -> Path | None:
    """Resolve a path under the static directory, rejecting traversal attempts."""
    if not full_path:
        return None
    candidate = (STATIC_DIR / full_path).resolve()
    static_root = STATIC_DIR.resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError:
        return None
    return candidate


def _index_response() -> FileResponse:
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(503, FRONTEND_HINT)
    # The shell names content-hashed bundles; a cached copy outlives them
    # after an upgrade and the portal then fails to load.
    return FileResponse(index, headers={"Cache-Control": "no-store"})


def mount_spa(app: FastAPI) -> None:
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/")
    def serve_index():
        return _index_response()

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        if full_path in {"api", "ws"} or full_path.startswith(("ws/", "api/")):
            raise HTTPException(404)
        if ".." in Path(full_path).parts:
            raise HTTPException(404)
        file_path = safe_static_path(full_path)
        if file_path is not None and file_path.is_file():
            if file_path.name == "index.html":
                return _index_response()
            return FileResponse(file_path)
        # Client routes have no extension; a missing file is a 404, not the shell.
        if "." in full_path.rsplit("/", 1)[-1]:
            raise HTTPException(404)
        return _index_response()
