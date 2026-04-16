#!/usr/bin/env python3
"""Job Jigsaw Notifier — fetches scored jobs, sends email digest + Telegram ping."""

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from config import get_settings
from email_utils import build_email_html, send_email
from telegram import send_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

PROFILE_PATH = Path("/data/profile.yaml")
JOBS_DB = Path("/data/jobs.db")
SENT_DB = Path("/data/sent_jobs.db")


def load_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return yaml.safe_load(f)


# ── Sent-jobs tracking ────────────────────────────────────────────────────────

def init_sent_db() -> sqlite3.Connection:
    con = sqlite3.connect(SENT_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sent_jobs (
            job_url TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        )
    """)
    con.commit()
    return con


def fetch_unsent_jobs(profile: dict, sent_con: sqlite3.Connection) -> list[dict]:
    notif = profile["notification"]
    threshold = notif["score_threshold"]
    max_jobs = notif["max_jobs_per_email"]

    con = sqlite3.connect(JOBS_DB)
    con.row_factory = sqlite3.Row
    try:
        # No date filter — sent_jobs.db is the source of truth for deduplication.
        # Disliked jobs (user_rating = -1) are excluded.
        rows = con.execute("""
            SELECT title, employer, location, job_url, suitability_score,
                   date_posted, is_remote, job_type
            FROM jobs
            WHERE suitability_score >= ?
              AND (user_rating IS NULL OR user_rating != -1)
            ORDER BY suitability_score DESC
        """, (threshold,)).fetchall()
    except Exception as e:
        log.error("jobs.db query failed: %s", e)
        return []
    finally:
        con.close()

    sent = {
        r[0] for r in sent_con.execute("SELECT job_url FROM sent_jobs").fetchall()
    }
    results = []
    for r in rows:
        if r["job_url"] in sent:
            continue
        job = dict(r)
        job["company"] = job.pop("employer", "")
        job["score"] = job.pop("suitability_score", 0)
        results.append(job)
    return results[:max_jobs]


def mark_sent(con: sqlite3.Connection, jobs: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    con.executemany(
        "INSERT OR IGNORE INTO sent_jobs (job_url, sent_at) VALUES (?, ?)",
        [(j["job_url"], now) for j in jobs],
    )
    con.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== Job Jigsaw Notifier starting ===")
    settings = get_settings()
    profile = load_profile()
    notif = profile["notification"]

    sent_con = init_sent_db()
    jobs = fetch_unsent_jobs(profile, sent_con)
    log.info("Found %d unsent jobs above threshold.", len(jobs))

    if not jobs:
        log.info("Nothing to send today.")
        return

    date_str = datetime.now(ZoneInfo(notif["timezone"])).strftime("%B %d, %Y")
    top_score = jobs[0]["score"]

    subject = notif["email_subject"].format(count=len(jobs), top_score=top_score)
    html = build_email_html(jobs, date_str, settings.site_url)
    send_email(settings, subject, html)

    tg_text = notif["telegram_message"].format(count=len(jobs), top_score=top_score)
    send_message(settings, tg_text)

    mark_sent(sent_con, jobs)
    sent_con.close()
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
