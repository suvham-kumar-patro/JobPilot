"""
worker/job_navigator.py
-----------------------
Scrapes LinkedIn job listings using Playwright.
Now accepts auto-query from profile and captures posted_at date.

Usage:
    python -m worker.job_navigator --query "Python Developer" --location "Bengaluru" --limit 25
    python -m worker.job_navigator --from-profile          # uses profile's search_query
"""

import asyncio
import sqlite3
import os
import re
import json
import argparse
import random
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

DB_PATH      = os.getenv("DB_PATH", "backend/jobs.db")
RAW_JOB_PATH = os.getenv("RAW_JOB_PATH", "data/raw_job.txt")
HEADLESS     = os.getenv("HEADLESS", "true").lower() == "true"

LINKEDIN_SEARCH_URL = (
    "https://www.linkedin.com/jobs/search?"
    "keywords={keywords}&location={location}"
    "&f_TPR=r86400"   # posted in last 24 hours — change r86400→r604800 for 7 days
    "&sortBy=DD"       # sort by date
    "&position=1&pageNum=0"
)


async def _random_delay(min_s=1.2, max_s=3.5):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _scroll_to_bottom(page: Page, steps=5):
    for _ in range(steps):
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight / 5)")
        await asyncio.sleep(0.4)


async def scrape_linkedin(
    query: str,
    location: str = "India",
    limit: int = 25,
    db_path: str = DB_PATH,
) -> list[dict]:
    url = LINKEDIN_SEARCH_URL.format(
        keywords=quote_plus(query),
        location=quote_plus(location),
    )
    jobs: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()
        await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}",
                         lambda route: route.abort())

        print(f"[navigator] Searching: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await _random_delay(2, 4)
        await _scroll_to_bottom(page)

        card_selector = "a.base-card__full-link"
        try:
            await page.wait_for_selector(card_selector, timeout=15_000)
        except PlaywrightTimeout:
            print("[navigator] No job cards found — LinkedIn may have changed layout.")
            await browser.close()
            return []

        cards = await page.query_selector_all(card_selector)
        links = []
        for card in cards[:limit]:
            href = await card.get_attribute("href")
            if href:
                links.append(href.split("?")[0])
        links = list(dict.fromkeys(links))
        print(f"[navigator] Found {len(links)} job links.")

        for idx, job_url in enumerate(links, 1):
            print(f"[navigator] [{idx}/{len(links)}] {job_url}")
            job = await _scrape_job_detail(page, job_url)
            if job:
                jobs.append(job)
            await _random_delay(0.8, 1.5)

        await browser.close()

    _save_to_db(jobs, db_path)
    _save_raw_text(jobs)
    print(f"[navigator] Done. Scraped {len(jobs)} jobs.")
    return jobs


async def _scrape_job_detail(page: Page, url: str) -> Optional[dict]:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await _random_delay(1.0, 2.0)

        # Expand show more
        try:
            btn = await page.query_selector("button.show-more-less-html__button")
            if btn:
                await btn.click()
                await asyncio.sleep(0.5)
        except Exception:
            pass

        title    = await _safe_text(page, "h1.top-card-layout__title, h1.job-details-jobs-unified-top-card__job-title")
        company  = await _safe_text(page, "a.topcard__org-name-link, span.job-details-jobs-unified-top-card__company-name")
        location = await _safe_text(page, "span.topcard__flavor--bullet, span.job-details-jobs-unified-top-card__bullet")
        job_type = await _safe_text(page, "span.job-criteria__text--criteria")
        raw_text = await _safe_text(page, "div.show-more-less-html__markup, div.description__text", multi=True)

        # Capture posted_at from the time element
        posted_at = None
        try:
            time_el = await page.query_selector("span.posted-time-ago__text, span.job-details-jobs-unified-top-card__posted-date")
            if time_el:
                posted_at = (await time_el.inner_text()).strip()
        except Exception:
            pass

        if not title:
            return None

        return {
            "title":     title.strip(),
            "company":   (company or "").strip(),
            "location":  (location or "").strip(),
            "job_type":  (job_type or "").strip(),
            "url":       url,
            "source":    "linkedin",
            "raw_text":  (raw_text or "").strip(),
            "posted_at": posted_at,
            "scraped_at": datetime.utcnow().isoformat(),
            "status":    "new",
        }
    except PlaywrightTimeout:
        print(f"[navigator] Timeout: {url}")
        return None
    except Exception as e:
        print(f"[navigator] Error on {url}: {e}")
        return None


async def _safe_text(page: Page, selector: str, multi=False) -> str:
    try:
        if multi:
            els = await page.query_selector_all(selector)
            texts = [await el.inner_text() for el in els]
            return "\n".join(t.strip() for t in texts if t.strip())
        el = await page.query_selector(selector)
        return (await el.inner_text()).strip() if el else ""
    except Exception:
        return ""


def _save_to_db(jobs: list[dict], db_path: str) -> None:
    if not jobs:
        return
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    inserted = 0
    for j in jobs:
        try:
            c.execute(
                """INSERT OR IGNORE INTO jobs
                   (title, company, location, job_type, url, source, raw_text, posted_at, scraped_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (j["title"], j["company"], j["location"], j["job_type"],
                 j["url"], j["source"], j["raw_text"], j.get("posted_at"),
                 j["scraped_at"], j["status"]),
            )
            if c.rowcount:
                inserted += 1
        except sqlite3.Error as e:
            print(f"[navigator] DB error: {e}")
    conn.commit()
    conn.close()
    print(f"[navigator] Inserted {inserted} new jobs (duplicates skipped).")


def _save_raw_text(jobs: list[dict]) -> None:
    os.makedirs("data", exist_ok=True)
    with open(RAW_JOB_PATH, "w", encoding="utf-8") as f:
        for j in jobs:
            f.write(f"=== {j['title']} @ {j['company']} ===\n")
            f.write(f"Location : {j['location']}\n")
            f.write(f"Type     : {j['job_type']}\n")
            f.write(f"Posted   : {j.get('posted_at', 'unknown')}\n")
            f.write(f"URL      : {j['url']}\n\n")
            f.write(j["raw_text"] or "(no description)")
            f.write("\n\n" + "-" * 80 + "\n\n")


def get_query_from_profile(db_path: str = DB_PATH) -> tuple[str, str]:
    """Return (search_query, location_pref) from the active profile."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT raw_resume, role_target, location_pref FROM profiles ORDER BY updated_at DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if not row:
        return "Software Engineer", "India"
    raw_resume, role_target, location_pref = row
    if raw_resume:
        try:
            p = json.loads(raw_resume)
            q = p.get("search_query") or p.get("role_target") or "Software Engineer"
            loc = p.get("location_pref") or location_pref or "India"
            return q, loc
        except Exception:
            pass
    return role_target or "Software Engineer", location_pref or "India"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", type=str, default=None)
    parser.add_argument("--location", type=str, default=None)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--db", type=str, default=DB_PATH)
    parser.add_argument("--from-profile", action="store_true",
                        help="Auto-generate query from uploaded resume/profile")
    args = parser.parse_args()

    if args.from_profile or (not args.query):
        query, location = get_query_from_profile(args.db)
        print(f"[navigator] Using profile query: '{query}' in '{location}'")
    else:
        query = args.query
        location = args.location or "India"

    asyncio.run(scrape_linkedin(query, location, args.limit, args.db))