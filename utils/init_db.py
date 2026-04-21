import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "backend/jobs.db")


def init_db(db_path: str = DB_PATH) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")

    # ── jobs ──────────────────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        title        TEXT NOT NULL,
        company      TEXT,
        location     TEXT,
        job_type     TEXT,
        url          TEXT UNIQUE NOT NULL,
        source       TEXT DEFAULT 'linkedin',
        raw_text     TEXT,
        posted_at    TEXT,
        scraped_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status       TEXT DEFAULT 'new'
    )""")

    # ── profiles ──────────────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS profiles (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        skills        TEXT,
        experience    TEXT,
        role_target   TEXT,
        location_pref TEXT,
        raw_resume    TEXT,
        updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── match_scores ──────────────────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS match_scores (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id         INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        profile_id     INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        score          REAL CHECK(score >= 0 AND score <= 1),
        ats_score      REAL,
        reasoning      TEXT,
        matched_skills TEXT,
        gaps           TEXT,
        scored_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(job_id, profile_id)
    )""")

    # ── indexes ───────────────────────────────────────────────────────────────
    c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_scraped   ON jobs(scraped_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_posted    ON jobs(posted_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scores_job     ON match_scores(job_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scores_score   ON match_scores(score DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scores_profile ON match_scores(profile_id)")

    # ── safe migrations (add missing columns to existing tables) ──────────────
    existing_jobs = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
    if "posted_at" not in existing_jobs:
        c.execute("ALTER TABLE jobs ADD COLUMN posted_at TEXT")
        print("[init_db] Migration: added posted_at to jobs")

    existing_ms = {r[1] for r in c.execute("PRAGMA table_info(match_scores)").fetchall()}
    if "ats_score" not in existing_ms:
        c.execute("ALTER TABLE match_scores ADD COLUMN ats_score REAL")
        print("[init_db] Migration: added ats_score to match_scores")
    if "gaps" not in existing_ms:
        c.execute("ALTER TABLE match_scores ADD COLUMN gaps TEXT")
        print("[init_db] Migration: added gaps to match_scores")

    conn.commit()
    conn.close()
    print(f"[init_db] Database ready at: {db_path}")


if __name__ == "__main__":
    init_db()