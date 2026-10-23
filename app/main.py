
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.legacy_routes import router as legacy_router
from app.api.campaign_routes import router as campaign_router
from app.config import PROJECT_ROOT


UI_DIR = PROJECT_ROOT / "ui"
UI_DIST = UI_DIR / "dist"
UI_ASSETS = UI_DIST / "assets"


app = FastAPI(
    title="Mania Mobiles Lead Qualification API",
    description=(
        "Customer conversations, automatic lead qualification, "
        "product management, and WhatsApp campaigns"
    ),
    version="1.2.0",
)


# --------------------------------------------------
# API ROUTERS
# --------------------------------------------------

# Original authentication, lead management, chat, and
# WhatsApp webhook endpoints.
app.include_router(
    legacy_router,
    prefix="/api",
)

# Product management, campaign preview, campaign sending,
# and campaign response endpoints.
app.include_router(
    campaign_router,
    prefix="/api",
)


# --------------------------------------------------
# API ROUTE VALIDATION
# --------------------------------------------------

def find_duplicate_api_routes():
    """
    Return duplicate API method/path pairs.

    This is diagnostic only: it does not modify or remove routes.
    """
    route_counts = Counter()

    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()

        if path.startswith("/api/"):
            for method in methods:
                route_counts[(method, path)] += 1

    return [
        (method, path, count)
        for (method, path), count in route_counts.items()
        if count > 1
    ]


# --------------------------------------------------
# REACT STATIC ASSETS
# --------------------------------------------------

# Mount the production React assets when the build exists.
if UI_ASSETS.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=UI_ASSETS),
        name="assets",
    )


# --------------------------------------------------
# FRONTEND HELPERS
# --------------------------------------------------

def _frontend_index() -> Optional[Path]:
    """Return the built React entry point if it exists."""
    index = UI_DIST / "index.html"
    return index if index.is_file() else None


# --------------------------------------------------
# HOME
# --------------------------------------------------

@app.get("/")
def home():
    """Serve React in production or API information in development."""
    index = _frontend_index()

    if index:
        return FileResponse(index)

    return {
        "message": "Mania Mobiles Lead Qualification API is running",
        "docs": "/docs",
        "health": "/health",
        "dashboard": (
            "Run the React UI with `cd ui && npm run dev` "
            "or build it using `cd ui && npm run build`."
        ),
    }


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.get("/health")
def health():
    """Basic application health endpoint."""
    return {"status": "healthy"}


# --------------------------------------------------
# DASHBOARD
# --------------------------------------------------

@app.get("/dashboard")
def dashboard():
    """Serve the built React dashboard."""
    index = _frontend_index()

    if not index:
        raise HTTPException(
            status_code=503,
            detail=(
                "Frontend is not built. "
                "Run `cd ui && npm run build`."
            ),
        )

    return FileResponse(index)


# --------------------------------------------------
# REACT ROUTER FALLBACK
# --------------------------------------------------

@app.get("/{full_path:path}")
def frontend_fallback(full_path: str):
    """
    Serve React Router paths without swallowing API, docs,
    health-check, or OpenAPI endpoints.
    """
    reserved_prefixes = (
        "api/",
        "docs",
        "redoc",
        "openapi.json",
        "health",
    )

    if full_path.startswith(reserved_prefixes):
        raise HTTPException(
            status_code=404,
            detail="Not found",
        )

    index = _frontend_index()

    if not index:
        raise HTTPException(
            status_code=404,
            detail="Not found",
        )

    # Resolve the requested path and ensure it stays inside UI_DIST.
    requested_file = (UI_DIST / full_path).resolve()
    resolved_dist = UI_DIST.resolve()

    try:
        requested_file.relative_to(resolved_dist)
        is_inside_dist = True
    except ValueError:
        is_inside_dist = False

    if is_inside_dist and requested_file.is_file():
        return FileResponse(requested_file)

    # Let React handle client-side routes.
    return FileResponse(index)
