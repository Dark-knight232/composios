"""FastAPI backend for the Composio 100-App Product Ops case study.

Deploy as a Render Web Service:
  - Build command: pip install -r requirements-backend.txt
  - Start command: uvicorn backend.main:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Composio Product Ops Research Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data directory: repo root / data
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load_json(filename: str) -> Any:
    path = DATA_DIR / filename
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


class ResearchAppRequest(BaseModel):
    app_name: str
    force_refresh: bool = False


@app.get("/health")
async def health() -> dict[str, Any]:
    files = sorted(p.name for p in DATA_DIR.glob("*.json")) if DATA_DIR.exists() else []
    return {
        "status": "ok",
        "data_dir": str(DATA_DIR),
        "data_files": files,
        "composio_configured": bool(os.getenv("COMPOSIO_API_KEY")),
        "nim_configured": bool(os.getenv("NIM_API_KEY")),
    }


@app.get("/api/dataset")
async def get_dataset() -> dict[str, Any]:
    apps = _load_json("final_verified_apps.json") or []
    patterns = _load_json("case_study_patterns.json") or {}
    audit = _load_json("audit_report.json") or {}
    return {"apps": apps, "patterns": patterns, "audit_report": audit}


@app.post("/api/research-app")
async def research_app(req: ResearchAppRequest) -> dict[str, Any]:
    """Look up (or stub) a single app record.

    When COMPOSIO_API_KEY + NIM_API_KEY are set, this would run the full
    Pass 1 pipeline. Without them it returns a clear, structured error so
    the Vercel frontend can fall back to the static dataset gracefully.
    """
    apps = _load_json("final_verified_apps.json") or []
    existing = next(
        (a for a in apps if a.get("app_name", "").lower() == req.app_name.lower()),
        None,
    )
    if existing and not req.force_refresh:
        return {"found": True, "record": existing, "source": "cached"}

    composio_key = os.getenv("COMPOSIO_API_KEY", "")
    nim_key = os.getenv("NIM_API_KEY", "")

    if not composio_key or not nim_key:
        return {
            "found": False,
            "record": None,
            "error": "Backend not configured",
            "hint": "Set COMPOSIO_API_KEY and NIM_API_KEY env vars and redeploy to enable live research",
        }

    # Live research path: delegate to the pipeline
    try:
        from src.agent import ResearchAgent  # type: ignore
    except Exception:
        return {
            "found": False,
            "record": None,
            "error": "Pipeline module unavailable on this deployment",
            "hint": "Ensure src/ is included in the Render build context",
        }

    return {
        "found": False,
        "record": None,
        "error": "Live research not yet wired to this endpoint",
        "hint": "Use the static dataset or extend this endpoint with src.agent.ResearchAgent",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))