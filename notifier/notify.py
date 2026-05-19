#!/usr/bin/env python3
"""Job Jigsaw Notifier — fetches scored jobs, sends email digest + Telegram ping."""
from __future__ import annotations

import json
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
                   COALESCE(description, '') as description,
                   COALESCE(site, '') as site
            FROM jobs
            WHERE suitability_score >= ?
              AND (user_rating IS NULL OR user_rating != -1)
              AND (hidden = 0 OR hidden IS NULL)
              AND (status IS NULL OR status = 'interested')
              AND (
                -- E1: interested status bypasses staleness filter entirely
                status = 'interested'
                OR (
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

def rerank_with_jina(jobs: list[dict], profile: dict, settings) -> list[dict] | None:
    """Re-rank jobs using Jina Reranker v3. Returns None when skipped or on failure."""
    if not getattr(settings, "jina_api_key", "") or not jobs:
        return None

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
        log.warning("Jina reranker failed: %s", e)
        return None


def rerank_with_llm(jobs: list[dict], profile: dict, settings) -> list[dict] | None:
    """Re-rank jobs via OpenRouter LLM. Returns None when skipped or on failure."""
    if not getattr(settings, "openrouter_api_key", "") or not jobs:
        return None

    notif = profile.get("notification", {})
    rerank_min_score = notif.get("rerank_min_score", 0.3)

    resume = profile.get("resume", "")[:800]
    feedback = profile.get("feedback_summary", "")
    candidate = f"{resume}\n{feedback}".strip()

    job_lines = []
    for i, job in enumerate(jobs):
        desc = (job.get("description") or job.get("reason") or "")[:300]
        job_lines.append(
            f'{i}. {job.get("title")} at {job.get("company")} ({job.get("location")}): {desc}'
        )

    prompt = (
        "Rank these job postings by relevance to this candidate. "
        "Return ONLY valid JSON with no explanation.\n\n"
        f"CANDIDATE:\n{candidate}\n\n"
        "JOBS:\n" + "\n".join(job_lines) + "\n\n"
        'Return: {"ranked": [indices best→worst], "scores": [relevance 0-1 per ranked job]}'
    )

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.openrouter_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        result = json.loads(content.strip())
        ranked_indices = result.get("ranked", [])
        scores = result.get("scores", [])

        reranked = []
        for pos, idx in enumerate(ranked_indices):
            if not isinstance(idx, int) or idx < 0 or idx >= len(jobs):
                continue
            score = scores[pos] if pos < len(scores) else 1.0
            if score < rerank_min_score:
                log.info("LLM re-ranker dropped job (score %.2f): %s", score, jobs[idx].get("title"))
                continue
            reranked.append(jobs[idx])

        if not reranked:
            return None

        log.info("LLM re-ranked %d → %d jobs", len(jobs), len(reranked))
        return reranked

    except Exception as e:
        log.warning("LLM re-ranker failed: %s", e)
        return None


def rerank_jobs(jobs: list[dict], profile: dict, settings) -> list[dict]:
    """Jina first, OpenRouter LLM as fallback, score order as last resort."""
    result = rerank_with_jina(jobs, profile, settings)
    if result is not None:
        return result
    result = rerank_with_llm(jobs, profile, settings)
    if result is not None:
        log.info("Used LLM re-ranker (Jina unavailable)")
        return result
    log.info("No re-ranker available — using score order")
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

    jobs = rerank_jobs(candidates, profile, settings)

    # A6 — Per-source budget: cap jobs per source before final trim
    linkedin_max = notif.get("linkedin_max_jobs", None)
    indeed_max = notif.get("indeed_max_jobs", None)
    rss_sources = {"weworkremotely", "remotive", "jobicy", "remoteok"}
    ats_sources = {"greenhouse", "lever", "ashby"}
    rss_max = notif.get("rss_max_jobs", None)
    ats_max = notif.get("ats_max_jobs", None)

    if any(v is not None for v in [linkedin_max, indeed_max, rss_max, ats_max]):
        source_counts: dict[str, int] = {}
        budgeted: list[dict] = []
        for job in jobs:
            site = (job.get("site") or "").lower()
            if site == "linkedin" and linkedin_max is not None:
                if source_counts.get("linkedin", 0) >= linkedin_max:
                    continue
            elif site == "indeed" and indeed_max is not None:
                if source_counts.get("indeed", 0) >= indeed_max:
                    continue
            elif site in rss_sources and rss_max is not None:
                if source_counts.get("rss", 0) >= rss_max:
                    continue
                site = "rss"  # group for counting
            elif site in ats_sources and ats_max is not None:
                if source_counts.get("ats", 0) >= ats_max:
                    continue
                site = "ats"
            source_counts[site] = source_counts.get(site, 0) + 1
            budgeted.append(job)
        log.info("Per-source budget: %d → %d jobs (budgets: linkedin=%s indeed=%s rss=%s ats=%s)",
                 len(jobs), len(budgeted), linkedin_max, indeed_max, rss_max, ats_max)
        jobs = budgeted

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
