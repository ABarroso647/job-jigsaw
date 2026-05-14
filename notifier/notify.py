#!/usr/bin/env python3
"""Job Jigsaw Notifier — fetches scored jobs, sends email digest + Telegram ping."""
from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
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

STALE_DATE_POSTED_DAYS = 14   # drop jobs where date_posted is known and older than this
JINA_RERANK_URL = "https://api.jina.ai/v1/rerank"
JINA_RERANK_MODEL = "jina-reranker-v3"
JINA_TIMEOUT = 30


def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        log.error("profile.yaml not found at %s — set up your profile via the web UI first", PROFILE_PATH)
        sys.exit(1)
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
    notif = profile.get("notification", {})
    threshold = notif.get("score_threshold", 60)
    max_job_age_days = notif.get("max_job_age_days", 7)
    rerank_candidates = notif.get("rerank_candidates", 30)

    con = sqlite3.connect(JOBS_DB)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("""
            SELECT title, employer, location, job_url, suitability_score,
                   suitability_reason, date_posted, is_remote, job_type,
                   COALESCE(description, '') as description
            FROM jobs
            WHERE suitability_score >= ?
              AND (user_rating IS NULL OR user_rating != -1)
              AND (hidden = 0 OR hidden IS NULL)
              AND (
                -- Primary: known date_posted must be within 14 days
                (
                  date_posted IS NOT NULL
                  AND date_posted != 'nan'
                  AND date_posted != ''
                  AND date(date_posted) >= date('now', '-' || ? || ' days')
                )
                OR
                -- Fallback: when date_posted is unknown, use discovered_at age
                (
                  (date_posted IS NULL OR date_posted = 'nan' OR date_posted = '')
                  AND discovered_at >= datetime('now', '-' || ? || ' days')
                )
              )
            ORDER BY suitability_score DESC
        """, (threshold, STALE_DATE_POSTED_DAYS, max_job_age_days)).fetchall()
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
        job["reason"] = job.pop("suitability_reason", "")
        results.append(job)

    return results[:rerank_candidates]


# ── Jina Reranker ─────────────────────────────────────────────────────────────

def rerank_with_jina(jobs: list[dict], profile: dict, settings) -> list[dict]:
    """Re-rank jobs using Jina Reranker v3. Falls back to original order on any failure."""
    if not getattr(settings, "jina_api_key", "") or not jobs:
        return jobs

    notif = profile.get("notification", {})
    rerank_min_score = notif.get("rerank_min_score", 0.3)

    query_parts = [profile.get("resume", "")[:1000]]
    if profile.get("feedback_summary"):
        query_parts.append(profile["feedback_summary"])
    query = "\n".join(query_parts).strip()

    documents = []
    for job in jobs:
        desc = (job.get("description") or "")[:500]
        reason = (job.get("reason") or "")
        if not desc:
            desc = reason
        doc = f"{job.get('title', '')} at {job.get('company', '')} ({job.get('location', '')})\n{desc}"
        documents.append(doc)

    try:
        resp = requests.post(
            JINA_RERANK_URL,
            headers={
                "Authorization": f"Bearer {settings.jina_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": JINA_RERANK_MODEL,
                "query": query,
                "documents": documents,
                "top_n": len(jobs),
            },
            timeout=JINA_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])

        reranked = []
        for item in results:
            idx = item.get("index")
            score = item.get("relevance_score", 0.0)
            if score < rerank_min_score:
                log.info("Jina dropped job (score %.2f < %.2f): %s",
                         score, rerank_min_score, jobs[idx].get("title"))
                continue
            reranked.append(jobs[idx])

        log.info("Jina re-ranked %d → %d jobs (min_score=%.2f)",
                 len(jobs), len(reranked), rerank_min_score)
        return reranked

    except Exception as e:
        log.warning("Jina reranker failed, using original order: %s", e)
        return jobs


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
    max_jobs = notif.get("max_jobs_per_email", 5)

    sent_con = init_sent_db()
    candidates = fetch_unsent_jobs(profile, sent_con)
    log.info("Found %d unsent candidates above threshold.", len(candidates))

    if not candidates:
        log.info("Nothing to send today.")
        sent_con.close()
        return

    jobs = rerank_with_jina(candidates, profile, settings)
    jobs = jobs[:max_jobs]

    if not jobs:
        log.info("All candidates filtered by re-ranker.")
        sent_con.close()
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
