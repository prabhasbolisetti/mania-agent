from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import PROJECT_ROOT

UI_DIR = PROJECT_ROOT / "ui"
UI_DIST = UI_DIR / "dist"
UI_ASSETS = UI_DIST / "assets"

app = FastAPI(
    title="Mania Mobiles Lead Qualification API",
    description="Customer conversations and automatic lead qualification",
    version="1.1.0",
)

app.include_router(router, prefix="/api")

# The React app is built during deployment. Mount its hashed assets only when
# the build exists, so importing the API during local development still works.
if UI_ASSETS.is_dir():
    app.mount("/assets", StaticFiles(directory=UI_ASSETS), name="assets")


def _frontend_index() -> Optional[Path]:
    index = UI_DIST / "index.html"
    return index if index.is_file() else None


@app.get("/")
def home():
    index = _frontend_index()
    if index:
        return FileResponse(index)
    return {
        "message": "Mania Mobiles Lead Qualification API is running",
        "docs": "/docs",
        "dashboard": "Run the React UI with `cd ui && npm run dev` or build `ui/dist`.",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/dashboard")
def dashboard():
    index = _frontend_index()
    if not index:
        raise HTTPException(
            status_code=503,
            detail="Frontend is not built. Run `cd ui && npm run build`.",
        )
    return FileResponse(index)


@app.get("/{full_path:path}")
def frontend_fallback(full_path: str):
    """Serve React Router paths without swallowing API/docs endpoints."""
    if full_path.startswith(("api/", "docs", "redoc", "openapi.json", "health")):
        raise HTTPException(status_code=404, detail="Not found")

    index = _frontend_index()
    if index:
        requested_file = (UI_DIST / full_path).resolve()
        # Prevent path traversal and serve existing built files when requested.
        if requested_file.is_relative_to(UI_DIST.resolve()) and requested_file.is_file():
            return FileResponse(requested_file)
        return FileResponse(index)

    raise HTTPException(status_code=404, detail="Not found")
