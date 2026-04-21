"""
worker/matcher.py
-----------------
ATS scoring + gap analysis with learning time estimates using Gemini.

Usage:
    python -m worker.matcher
    python -m worker.matcher --job-id 42
    python -m worker.matcher --limit 10
"""

import os
import json
import sqlite3
import argparse
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from google import genai

DB_PATH = os.getenv("DB_PATH", "backend/jobs.db")
MODEL   = "gemini-2.0-flash"

_client = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        _client = genai.Client(api_key=key)
    return _client


# ---------------------------------------------------------------------------
# ATS Scoring prompt
# ---------------------------------------------------------------------------

ATS_PROMPT = """You are an ATS (Applicant Tracking System) evaluator and career coach.

Given a candidate profile and job description, return ONLY a valid JSON object — no markdown, no extra text:
{
  "score": <float 0.0 to 1.0>,
  "ats_score": <integer 0 to 100>,
  "reasoning": "<2-3 sentences on overall fit>",
  "matched_skills": ["skill present in both resume and JD"],
  "gaps": [
    {
      "skill": "skill name",
      "importance": "critical|important|nice-to-have",
      "learn_days": <realistic integer — days to reach working proficiency>,
      "resource": "best free resource to learn this (course name or site)"
    }
  ]
}

ATS score guide:
- 85-100: Excellent — likely to pass ATS filters
- 70-84:  Good — competitive candidate
- 50-69:  Moderate — some gaps but worth applying
- below 50: Poor fit

For learn_days be realistic:
- A new programming language: 30-90 days
- A framework (React, FastAPI): 14-30 days
- A cloud service (AWS S3): 7-14 days
- A tool (Docker, Git): 3-7 days
- A concept (REST APIs): 2-5 days"""


def score_job(profile: dict, job: dict) -> dict:
    import time
    profile_text = json.dumps(profile, indent=2)
    job_text = (
        f"Title   : {job['title']}\n"
        f"Company : {job['company']}\n"
        f"Location: {job['location']}\n"
        f"Type    : {job['job_type']}\n\n"
        f"Description:\n{(job['raw_text'] or '')[:4000]}"
    )
    prompt = f"{ATS_PROMPT}\n\nCandidate Profile:\n{profile_text}\n\nJob:\n{job_text}"

    for attempt in range(3):
        try:
            response = _get_client().models.generate_content(model=MODEL, contents=prompt)
            raw = response.text.strip()
            break
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                wait = 35 * (attempt + 1)
                print(f"[matcher] Rate limited — waiting {wait}s before retry {attempt+1}/3...")
                time.sleep(wait)
                if attempt == 2:
                    print("[matcher] Max retries hit. Skipping this job.")
                    return {"score": 0.0, "ats_score": 0, "reasoning": "Rate limit exceeded",
                            "matched_skills": [], "gaps": []}
            else:
                print(f"[matcher] API error: {e}")
                return {"score": 0.0, "ats_score": 0, "reasoning": "API error",
                        "matched_skills": [], "gaps": []}

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {
            "score": 0.0, "ats_score": 0,
            "reasoning": "Parse error", "matched_skills": [],
            "gaps": []
        }

    if "ats_score" not in result and "score" in result:
        result["ats_score"] = int(result["score"] * 100)

    return result


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_active_profile(conn):
    c = conn.cursor()
    c.execute("SELECT id, skills, experience, role_target, location_pref, raw_resume FROM profiles ORDER BY updated_at DESC LIMIT 1")
    row = c.fetchone()
    if not row:
        return None
    profile_id, skills, experience, role_target, location_pref, raw_resume = row
    if raw_resume:
        try:
            return profile_id, json.loads(raw_resume)
        except Exception:
            pass
    return profile_id, {
        "skills":        json.loads(skills or "[]"),
        "experience":    json.loads(experience or "{}"),
        "role_target":   role_target,
        "location_pref": location_pref,
    }


def _get_unscored_jobs(conn, profile_id, limit, job_id):
    c = conn.cursor()
    if job_id:
        c.execute("SELECT id, title, company, location, job_type, raw_text FROM jobs WHERE id=?", (job_id,))
    else:
        c.execute(
            """SELECT j.id, j.title, j.company, j.location, j.job_type, j.raw_text
               FROM jobs j
               WHERE j.status = 'new'
               AND NOT EXISTS (
                   SELECT 1 FROM match_scores ms
                   WHERE ms.job_id = j.id AND ms.profile_id = ?
               )
               ORDER BY j.posted_at DESC, j.scraped_at DESC
               LIMIT ?""",
            (profile_id, limit),
        )
    rows = c.fetchall()
    return [
        {"id": r[0], "title": r[1], "company": r[2],
         "location": r[3], "job_type": r[4], "raw_text": r[5] or ""}
        for r in rows
    ]


def _save_score(conn, job_id, profile_id, result):
    c = conn.cursor()
    c.execute(
        """INSERT INTO match_scores
               (job_id, profile_id, score, ats_score, reasoning, matched_skills, gaps, scored_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id, profile_id) DO UPDATE SET
               score=excluded.score,
               ats_score=excluded.ats_score,
               reasoning=excluded.reasoning,
               matched_skills=excluded.matched_skills,
               gaps=excluded.gaps,
               scored_at=excluded.scored_at""",
        (
            job_id, profile_id,
            result.get("score", 0.0),
            result.get("ats_score", 0),
            result.get("reasoning", ""),
            json.dumps(result.get("matched_skills", [])),
            json.dumps(result.get("gaps", [])),
            datetime.utcnow().isoformat(),
        ),
    )
    c.execute("UPDATE jobs SET status='matched' WHERE id=?", (job_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(db_path=DB_PATH, limit=50, job_id=None):
    conn = sqlite3.connect(db_path)
    profile_result = _get_active_profile(conn)
    if not profile_result:
        print("[matcher] No profile found. Upload your resume first.")
        conn.close()
        return []

    profile_id, profile = profile_result
    print(f"[matcher] Profile id={profile_id}, role='{profile.get('role_target', '?')}'")

    jobs = _get_unscored_jobs(conn, profile_id, limit, job_id)
    print(f"[matcher] Scoring {len(jobs)} jobs...")

    results = []
    for idx, job in enumerate(jobs, 1):
        if not job["raw_text"]:
            print(f"  [{idx}/{len(jobs)}] Skipping (no description): {job['title']}")
            continue
        print(f"  [{idx}/{len(jobs)}] {job['title']} @ {job['company']}")
        result = score_job(profile, job)
        _save_score(conn, job["id"], profile_id, result)
        ats = result.get("ats_score", 0)
        gaps = result.get("gaps", [])
        critical = [g["skill"] for g in gaps if g.get("importance") == "critical"]
        print(f"           ATS: {ats}% | Critical gaps: {critical or 'none'}")
        results.append({"job": job, **result})

    conn.close()
    print(f"[matcher] Done. Scored {len(results)} jobs.")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--job-id", type=int, default=None)
    args = parser.parse_args()
    run(args.db, args.limit, args.job_id)