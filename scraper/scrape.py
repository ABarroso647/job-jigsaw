#!/usr/bin/env python3
"""Job Jigsaw Scraper — scrapes job boards, scores via OpenRouter, stores in jobs.db."""

import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from jobspy import scrape_jobs

from config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

PROFILE_PATH = Path("/data/profile.yaml")
JOBS_DB = Path("/data/jobs.db")

MAX_RETRY_ATTEMPTS = 3
BASE_RETRY_WAIT = 5          # seconds; actual wait = BASE_RETRY_WAIT * (2 ** attempt)
JOB_DESCRIPTION_MAX_CHARS = 3000
RATE_LIMIT_STATUS_CODES = (429, 502, 503)
JOB_BOARDS = ["indeed", "linkedin"]
INDEED_COUNTRY = "Canada"
DEEP_RESULTS_PER_SITE = 50
OPENROUTER_TIMEOUT = 60      # seconds

SCORING_PROMPT = """\
You are evaluating a job listing for a candidate. Score suitability 0-100 using:
- Skills/experience match: 0-30 pts
- Experience level match: 0-25 pts
- Location/remote alignment: 0-15 pts
- Industry/domain fit: 0-15 pts
- Career growth potential: 0-15 pts

CANDIDATE PROFILE:
{profile_json}

JOB:
Title: {title}
Employer: {employer}
Location: {location}
Description: {description}

Return ONLY valid JSON: {{"score": <0-100>, "reason": "<1-2 sentences>"}}"""


# ── DB ────────────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(JOBS_DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id                 TEXT PRIMARY KEY,
            title              TEXT NOT NULL,
            employer           TEXT,
            location           TEXT,
            job_url            TEXT UNIQUE NOT NULL,
            suitability_score  REAL DEFAULT 0,
            suitability_reason TEXT,
            date_posted        TEXT,
            is_remote          INTEGER DEFAULT 0,
            job_type           TEXT,
            discovered_at      TEXT,
            user_rating        INTEGER DEFAULT NULL
        )
    """)
    con.commit()
    return con


def known_urls(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT job_url FROM jobs").fetchall()}


def insert_job(con: sqlite3.Connection, job: dict) -> None:
    con.execute("""
        INSERT OR IGNORE INTO jobs
            (id, title, employer, location, job_url, suitability_score,
             suitability_reason, date_posted, is_remote, job_type,
             discovered_at)
        VALUES
            (:id, :title, :employer, :location, :job_url, :suitability_score,
             :suitability_reason, :date_posted, :is_remote, :job_type,
             :discovered_at)
    """, job)
    con.commit()


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_job(job: dict, profile: dict, settings) -> tuple[float, str]:
    profile_json = json.dumps({
        "resume": profile.get("resume", ""),
        "description": profile.get("description", ""),
    }, ensure_ascii=False)

    prompt = SCORING_PROMPT.format(
        profile_json=profile_json,
        title=job.get("title", ""),
        employer=job.get("employer", ""),
        location=job.get("location", ""),
        description=(job.get("description") or "")[:JOB_DESCRIPTION_MAX_CHARS],
    )

    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                json={
                    "model": settings.openrouter_model,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=OPENROUTER_TIMEOUT,
            )

            if resp.status_code in RATE_LIMIT_STATUS_CODES:
                wait = BASE_RETRY_WAIT * (2 ** attempt)
                log.warning("Scoring rate-limited (%s) for '%s', retrying in %ds (attempt %d/%d)",
                            resp.status_code, job.get("title"), wait, attempt + 1, MAX_RETRY_ATTEMPTS)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]

            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            try:
                data = json.loads(content)
            except json.JSONDecodeError as e:
                log.warning("Bad JSON from model for '%s': %s | raw: %.200s",
                            job.get("title"), e, content)
                return 0.0, "Scoring unavailable"

            score = max(0, min(100, int(data["score"])))
            reason = str(data.get("reason", ""))
            return float(score), reason

        except requests.exceptions.RequestException as e:
            log.warning("Scoring request failed for '%s' (attempt %d/%d): %s",
                        job.get("title"), attempt + 1, MAX_RETRY_ATTEMPTS, e)
            if attempt < MAX_RETRY_ATTEMPTS - 1:
                time.sleep(BASE_RETRY_WAIT * (2 ** attempt))

    return 0.0, "Scoring unavailable"


# ── Scrape ────────────────────────────────────────────────────────────────────

def run(profile: dict, settings, deep: bool = False) -> None:
    search = profile.get("search", {})
    terms = search.get("terms", [])
    locations = search.get("locations", ["Toronto, ON"])
    results_per_site = search.get("results_per_site", 25) if not deep else DEEP_RESULTS_PER_SITE

    if deep:
        log.info("=== Deep search mode — no time filter, results_wanted=%d ===", results_per_site)
        hours_old_param = {}  # omit hours_old entirely → no time restriction
    else:
        hours_old = search.get("hours_old", 24)
        hours_old_param = {"hours_old": hours_old}

    con = init_db()
    seen = known_urls(con)
    new_total = 0

    for term in terms:
        for location in locations:
            log.info("Scraping: '%s' in %s", term, location)
            try:
                df = scrape_jobs(
                    site_name=JOB_BOARDS,
                    search_term=term,
                    location=location,
                    results_wanted=results_per_site,
                    country_indeed=INDEED_COUNTRY,
                    **hours_old_param,
                )
            except Exception as e:
                log.error("Scrape failed for '%s' in %s: %s", term, location, e)
                continue

            for _, row in df.iterrows():
                url = str(row.get("job_url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)

                job = {
                    "id": str(uuid.uuid4()),
                    "title": str(row.get("title") or ""),
                    "employer": str(row.get("company") or ""),
                    "location": str(row.get("location") or ""),
                    "job_url": url,
                    "date_posted": str(row.get("date_posted") or ""),
                    "is_remote": 1 if row.get("is_remote") else 0,
                    "job_type": str(row.get("job_type") or ""),
                    "description": str(row.get("description") or ""),  # used for scoring only
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                    "suitability_score": 0.0,
                    "suitability_reason": "",
                }

                log.info("Scoring: %s @ %s", job["title"], job["employer"])
                job["suitability_score"], job["suitability_reason"] = score_job(job, profile, settings)
                del job["description"]  # not stored in DB
                insert_job(con, job)
                new_total += 1

    con.close()
    log.info("Done — %d new jobs added.", new_total)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    deep = os.environ.get("DEEP_SEARCH") == "1"
    log.info("=== Job Jigsaw Scraper starting%s ===", " (deep)" if deep else "")
    settings = get_settings()
    if not PROFILE_PATH.exists():
        log.error("profile.yaml not found at %s — set up your profile via the web UI first", PROFILE_PATH)
        sys.exit(1)
    with open(PROFILE_PATH) as f:
        profile = yaml.safe_load(f)
    run(profile, settings, deep=deep)
    log.info("=== Scraper finished ===")


if __name__ == "__main__":
    main()
