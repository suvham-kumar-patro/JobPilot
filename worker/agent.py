"""
worker/agent.py
---------------
Reads a resume (PDF or plain text), extracts a structured profile using
Gemini, and auto-generates a LinkedIn search query from the profile.

Usage:
    python -m worker.agent --resume data/resume.pdf
    python -m worker.agent --resume data/resume.txt
"""

import os
import json
import sqlite3
import argparse
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from google import genai

DB_PATH      = os.getenv("DB_PATH", "backend/jobs.db")
PROFILE_PATH = os.getenv("PROFILE_PATH", "data/dynamic_profile.txt")
MODEL        = "gemini-2.0-flash"

_client = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set. Add it to your .env file.")
        _client = genai.Client(api_key=key)
    return _client


# ---------------------------------------------------------------------------
# Resume reader
# ---------------------------------------------------------------------------

def read_resume(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Resume not found: {path}")
    if p.suffix.lower() == ".pdf":
        try:
            from pdfminer.high_level import extract_text
            return extract_text(str(p))
        except ImportError:
            raise ImportError("pip install pdfminer.six")
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Profile extraction
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """You are a resume parser. Extract structured information from the resume.
Return ONLY a valid JSON object with exactly these keys — no markdown, no extra text:
{
  "name": "full name",
  "role_target": "most recent or desired job title (be specific, e.g. 'Senior Python Backend Engineer')",
  "location_pref": "preferred work location(s), city name only",
  "skills": ["skill1", "skill2", ...],
  "experience_years": <integer>,
  "experience_summary": "2-3 sentence summary",
  "education": "highest degree and institution",
  "languages": ["language1", ...],
  "keywords": ["keyword1", ...],
  "search_query": "a short 2-4 word LinkedIn job search query derived from role_target and top skills (e.g. 'Python Backend Developer', 'React Frontend Engineer')"
}"""


def extract_profile(resume_text: str) -> dict:
    print("[agent] Sending resume to Gemini for extraction...")
    prompt = f"{EXTRACT_PROMPT}\n\nResume:\n\n{resume_text}"
    response = _get_client().models.generate_content(model=MODEL, contents=prompt)
    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    try:
        profile = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"[agent] Gemini returned invalid JSON: {e}\nRaw:\n{raw}")
    return profile


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_profile_text(profile: dict) -> None:
    os.makedirs("data", exist_ok=True)
    lines = [
        f"Name             : {profile.get('name', '')}",
        f"Target role      : {profile.get('role_target', '')}",
        f"Search query     : {profile.get('search_query', '')}",
        f"Location pref    : {profile.get('location_pref', '')}",
        f"Experience (yrs) : {profile.get('experience_years', '')}",
        f"Education        : {profile.get('education', '')}",
        f"Languages        : {', '.join(profile.get('languages', []))}",
        "",
        "Skills:",
        *[f"  - {s}" for s in profile.get("skills", [])],
        "",
        "Keywords:",
        *[f"  - {k}" for k in profile.get("keywords", [])],
        "",
        "Experience summary:",
        f"  {profile.get('experience_summary', '')}",
        "",
        f"Generated at: {datetime.utcnow().isoformat()}",
    ]
    Path(PROFILE_PATH).write_text("\n".join(lines), encoding="utf-8")
    print(f"[agent] Profile saved to {PROFILE_PATH}")


def upsert_profile_db(profile: dict, db_path: str = DB_PATH) -> int:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT id FROM profiles ORDER BY updated_at DESC LIMIT 1")
    row = c.fetchone()
    skills_json = json.dumps(profile.get("skills", []))
    exp_json = json.dumps({
        "years":   profile.get("experience_years"),
        "summary": profile.get("experience_summary"),
    })
    if row:
        profile_id = row[0]
        c.execute(
            """UPDATE profiles SET skills=?, experience=?, role_target=?,
               location_pref=?, raw_resume=?, updated_at=? WHERE id=?""",
            (skills_json, exp_json, profile.get("role_target", ""),
             profile.get("location_pref", ""), json.dumps(profile),
             datetime.utcnow().isoformat(), profile_id),
        )
        print(f"[agent] Updated profile (id={profile_id}).")
    else:
        c.execute(
            """INSERT INTO profiles (skills, experience, role_target, location_pref, raw_resume, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (skills_json, exp_json, profile.get("role_target", ""),
             profile.get("location_pref", ""), json.dumps(profile),
             datetime.utcnow().isoformat()),
        )
        profile_id = c.lastrowid
        print(f"[agent] Inserted new profile (id={profile_id}).")
    conn.commit()
    conn.close()
    return profile_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(resume_path: str, db_path: str = DB_PATH) -> dict:
    resume_text = read_resume(resume_path)
    profile     = extract_profile(resume_text)
    save_profile_text(profile)
    upsert_profile_db(profile, db_path)
    return profile


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", required=True)
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()
    profile = run(args.resume, args.db)
    print(json.dumps(profile, indent=2))