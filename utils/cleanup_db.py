import sqlite3
import os
import argparse
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "backend/jobs.db")


def cleanup_old_jobs(db_path: str = DB_PATH, days: int = 30) -> int:
    """Delete jobs older than `days` days that were never applied to."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    cutoff = datetime.utcnow() - timedelta(days=days)
    c.execute(
        "DELETE FROM jobs WHERE scraped_at < ? AND status NOT IN ('applied')",
        (cutoff.isoformat(),),
    )
    deleted = c.rowcount
    conn.commit()
    conn.close()
    print(f"[cleanup_db] Deleted {deleted} stale jobs older than {days} days.")
    return deleted


def cleanup_low_scores(db_path: str = DB_PATH, threshold: float = 0.2) -> int:
    """Delete match_scores below threshold to keep the table lean."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("DELETE FROM match_scores WHERE score < ?", (threshold,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    print(f"[cleanup_db] Deleted {deleted} low-score entries (< {threshold}).")
    return deleted


def vacuum(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("VACUUM")
    conn.close()
    print("[cleanup_db] VACUUM complete.")


def full_cleanup(db_path: str = DB_PATH, days: int = 30, score_threshold: float = 0.2) -> None:
    cleanup_old_jobs(db_path, days)
    cleanup_low_scores(db_path, score_threshold)
    vacuum(db_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean up jobs.db")
    parser.add_argument("--days", type=int, default=30, help="Delete jobs older than N days")
    parser.add_argument("--score-threshold", type=float, default=0.2, help="Delete scores below this value")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Path to jobs.db")
    args = parser.parse_args()
    full_cleanup(args.db, args.days, args.score_threshold)