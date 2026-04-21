"""
backend/main.py  — Job Pilot FastAPI backend (enhanced)
"""

from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import (FastAPI, HTTPException, Query, UploadFile,
                     File, WebSocket, WebSocketDisconnect, BackgroundTasks)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.init_db import init_db
from worker.job_navigator import scrape_linkedin, get_query_from_profile
from worker import matcher as matcher_module
from worker import agent as agent_module

DB_PATH = os.getenv("DB_PATH", "backend/jobs.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DB_PATH)
    yield


app = FastAPI(title="Job Pilot API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/app", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class StatusUpdate(BaseModel):
    status: str

class ScrapeRequest(BaseModel):
    query: Optional[str] = None
    location: Optional[str] = None
    limit: int = 25
    from_profile: bool = True   # default: auto-use profile

class MatchRequest(BaseModel):
    limit: int = 50
    job_id: Optional[int] = None

class FullPipelineRequest(BaseModel):
    limit: int = 25
    from_profile: bool = True
    query: Optional[str] = None
    location: Optional[str] = None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@app.get("/api/jobs")
def list_jobs(
    status: Optional[str] = Query(None),
    min_ats: Optional[int] = Query(None, ge=0, le=100),
    search: Optional[str] = Query(None),
    sort_by: str = Query("ats_score", pattern="^(ats_score|score|scraped_at|posted_at|title|company)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    conn = get_conn()
    c = conn.cursor()

    filters, params = [], []
    if status:
        filters.append("j.status = ?"); params.append(status)
    if search:
        filters.append("(j.title LIKE ? OR j.company LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if min_ats is not None:
        filters.append("COALESCE(ms.ats_score, ms.score*100, 0) >= ?")
        params.append(min_ats)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    sort_map = {
        "ats_score": "COALESCE(ms.ats_score, ms.score*100)",
        "score": "ms.score",
        "scraped_at": "j.scraped_at",
        "posted_at": "j.posted_at",
        "title": "j.title",
        "company": "j.company",
    }
    sort_col = sort_map[sort_by]
    offset = (page - 1) * page_size

    rows = c.execute(f"""
        SELECT j.id, j.title, j.company, j.location, j.job_type,
               j.url, j.source, j.status, j.posted_at, j.scraped_at,
               ms.score, ms.ats_score, ms.reasoning, ms.matched_skills, ms.gaps
        FROM jobs j
        LEFT JOIN match_scores ms ON ms.job_id = j.id
        {where}
        ORDER BY {sort_col} {order.upper()} NULLS LAST
        LIMIT ? OFFSET ?
    """, params + [page_size, offset]).fetchall()

    total = c.execute(
        f"SELECT COUNT(*) FROM jobs j LEFT JOIN match_scores ms ON ms.job_id=j.id {where}",
        params
    ).fetchone()[0]

    conn.close()

    jobs = []
    for r in rows:
        ats = r["ats_score"] or (int(r["score"] * 100) if r["score"] else None)
        gaps_raw = r["gaps"]
        gaps = json.loads(gaps_raw) if gaps_raw else []
        jobs.append({
            "id":             r["id"],
            "title":          r["title"],
            "company":        r["company"],
            "location":       r["location"],
            "job_type":       r["job_type"],
            "url":            r["url"],
            "source":         r["source"],
            "status":         r["status"],
            "posted_at":      r["posted_at"],
            "scraped_at":     r["scraped_at"],
            "score":          round(r["score"], 3) if r["score"] is not None else None,
            "ats_score":      ats,
            "reasoning":      r["reasoning"],
            "matched_skills": json.loads(r["matched_skills"]) if r["matched_skills"] else [],
            "gaps":           gaps,
            "critical_gaps":  [g for g in gaps if g.get("importance") == "critical"],
            "total_learn_days": sum(g.get("learn_days", 0) for g in gaps),
        })

    return {"jobs": jobs, "total": total, "page": page, "page_size": page_size}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    conn = get_conn()
    row = conn.execute(
        """SELECT j.*, ms.score, ms.ats_score, ms.reasoning,
                  ms.matched_skills, ms.gaps, ms.scored_at
           FROM jobs j LEFT JOIN match_scores ms ON ms.job_id = j.id
           WHERE j.id = ?""", (job_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Job not found")
    d = dict(row)
    for f in ("matched_skills", "gaps"):
        if d.get(f):
            try: d[f] = json.loads(d[f])
            except: pass
    if not d.get("ats_score") and d.get("score"):
        d["ats_score"] = int(d["score"] * 100)
    return d


@app.post("/api/jobs/{job_id}/status")
def update_status(job_id: int, body: StatusUpdate):
    valid = {"new", "matched", "applied", "rejected", "saved"}
    if body.status not in valid:
        raise HTTPException(400, f"status must be one of {valid}")
    conn = get_conn()
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (body.status, job_id))
    conn.commit(); conn.close()
    return {"ok": True, "job_id": job_id, "status": body.status}


# ---------------------------------------------------------------------------
# Run endpoints
# ---------------------------------------------------------------------------

@app.post("/api/run/scrape")
def run_scrape(req: ScrapeRequest, background_tasks: BackgroundTasks):
    if req.from_profile and not req.query:
        query, location = get_query_from_profile(DB_PATH)
    else:
        query    = req.query or "Software Engineer"
        location = req.location or "India"
    background_tasks.add_task(_run_scrape_sync, query, location, req.limit)
    return {"ok": True, "message": f"Scraping '{query}' in '{location}' (latest 24h)"}


def _run_scrape_sync(query, location, limit):
    asyncio.run(scrape_linkedin(query, location, limit, DB_PATH))


@app.post("/api/run/match")
def run_match(req: MatchRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(matcher_module.run, DB_PATH, req.limit, req.job_id)
    return {"ok": True, "message": "ATS matching started in background."}


@app.post("/api/run/full")
async def run_full(req: FullPipelineRequest, background_tasks: BackgroundTasks):
    if req.from_profile and not req.query:
        query, location = get_query_from_profile(DB_PATH)
    else:
        query    = req.query or "Software Engineer"
        location = req.location or "India"

    def _full_pipeline_sync(q, loc, limit):
        asyncio.run(scrape_linkedin(q, loc, limit, DB_PATH))
        matcher_module.run(DB_PATH, limit=limit)

    background_tasks.add_task(_full_pipeline_sync, query, location, req.limit)
    return {"ok": True, "message": f"Pipeline started: '{query}' in '{location}'"}


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@app.get("/api/profile")
def get_profile():
    conn = get_conn()
    row = conn.execute("SELECT * FROM profiles ORDER BY updated_at DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "No profile found. Upload a resume first.")
    d = dict(row)
    for f in ("skills", "experience", "raw_resume"):
        if d.get(f):
            try: d[f] = json.loads(d[f])
            except: pass
    return d


@app.post("/api/profile/upload")
async def upload_resume(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".pdf", ".txt"}:
        raise HTTPException(400, "Only .pdf and .txt files are supported.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        profile = agent_module.run(tmp_path, DB_PATH)
    finally:
        os.unlink(tmp_path)
    return {
        "ok": True,
        "profile": profile,
        "auto_query": profile.get("search_query", profile.get("role_target")),
        "location":   profile.get("location_pref"),
    }


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def get_stats():
    conn = get_conn()
    c = conn.cursor()
    total       = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    new_jobs    = c.execute("SELECT COUNT(*) FROM jobs WHERE status='new'").fetchone()[0]
    matched     = c.execute("SELECT COUNT(*) FROM jobs WHERE status='matched'").fetchone()[0]
    applied     = c.execute("SELECT COUNT(*) FROM jobs WHERE status='applied'").fetchone()[0]
    top_ats     = c.execute("SELECT MAX(ats_score) FROM match_scores").fetchone()[0]
    avg_ats     = c.execute("SELECT AVG(ats_score) FROM match_scores WHERE ats_score IS NOT NULL").fetchone()[0]
    last_scrape = c.execute("SELECT MAX(scraped_at) FROM jobs").fetchone()[0]
    top_jobs    = c.execute(
        """SELECT j.id, j.title, j.company, j.location, j.url, j.posted_at,
                  ms.ats_score, ms.gaps
           FROM jobs j JOIN match_scores ms ON ms.job_id=j.id
           ORDER BY ms.ats_score DESC LIMIT 5"""
    ).fetchall()
    conn.close()
    return {
        "total_jobs":  total,
        "new":         new_jobs,
        "matched":     matched,
        "applied":     applied,
        "top_ats":     top_ats,
        "avg_ats":     round(avg_ats) if avg_ats else None,
        "last_scrape": last_scrape,
        "top_matches": [
            {
                "id": r["id"], "title": r["title"], "company": r["company"],
                "location": r["location"], "url": r["url"],
                "posted_at": r["posted_at"], "ats_score": r["ats_score"],
                "critical_gaps": [g for g in (json.loads(r["gaps"]) if r["gaps"] else [])
                                  if g.get("importance") == "critical"],
            }
            for r in top_jobs
        ],
    }


# ---------------------------------------------------------------------------
# WebSocket log stream
# ---------------------------------------------------------------------------

class LogBroadcaster:
    def __init__(self): self.connections = []
    async def connect(self, ws):
        await ws.accept(); self.connections.append(ws)
    def disconnect(self, ws):
        self.connections.remove(ws)
    async def broadcast(self, msg):
        for ws in list(self.connections):
            try: await ws.send_text(msg)
            except: self.connections.remove(ws)

broadcaster = LogBroadcaster()

@app.websocket("/ws/run")
async def ws_run(websocket: WebSocket):
    await broadcaster.connect(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)


@app.get("/")
def root():
    return {"message": "Job Pilot API v2. Visit /app for UI or /docs for Swagger."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)