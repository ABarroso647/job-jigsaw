"""RSS and JSON API job board sources for Job Jigsaw."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)

_NORMALIZED_KEYS = ("title", "company", "location", "job_url", "description",
                    "date_posted", "is_remote", "site")


def _normalize(
    title: str,
    company: str,
    location: str,
    job_url: str,
    description: str,
    date_posted: str | None,
    is_remote: bool,
    site: str,
) -> dict:
    return {
        "title": title or "",
        "company": company or "",
        "location": location or "",
        "job_url": job_url or "",
        "description": description or "",
        "date_posted": date_posted or "",
        "is_remote": is_remote,
        "site": site,
    }


# ── We Work Remotely (RSS) ────────────────────────────────────────────────────

_WWR_URL = "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss"


def _fetch_wwr() -> list[dict]:
    import feedparser  # lazy import — optional dep

    feed = feedparser.parse(_WWR_URL)
    jobs = []
    for entry in feed.entries:
        title = entry.get("title", "")
        # WWR titles look like "Company: Job Title" — split if so
        company = ""
        if ": " in title:
            parts = title.split(": ", 1)
            company = parts[0].strip()
            title = parts[1].strip()

        description = entry.get("summary", "") or entry.get("description", "") or ""
        job_url = entry.get("link", "") or entry.get("id", "")
        published = entry.get("published", "") or ""
        try:
            from email.utils import parsedate_to_datetime
            date_posted = parsedate_to_datetime(published).isoformat() if published else None
        except Exception:
            date_posted = published or None

        jobs.append(_normalize(
            title=title,
            company=company,
            location="Remote",
            job_url=job_url,
            description=description,
            date_posted=date_posted,
            is_remote=True,
            site="weworkremotely",
        ))
    log.info("WWR: fetched %d jobs", len(jobs))
    return jobs


# ── Remotive (JSON API) ───────────────────────────────────────────────────────

_REMOTIVE_URL = "https://remotive.com/api/remote-jobs?category=sales"


def _fetch_remotive() -> list[dict]:
    resp = requests.get(_REMOTIVE_URL, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for item in data.get("jobs", []):
        jobs.append(_normalize(
            title=item.get("title", ""),
            company=item.get("company_name", ""),
            location=item.get("candidate_required_location", "Remote"),
            job_url=item.get("url", ""),
            description=item.get("description", ""),
            date_posted=item.get("publication_date", ""),
            is_remote=True,
            site="remotive",
        ))
    log.info("Remotive: fetched %d jobs", len(jobs))
    return jobs


# ── Jobicy (JSON API) ─────────────────────────────────────────────────────────

_JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs?industry=marketing-sales"


def _fetch_jobicy() -> list[dict]:
    resp = requests.get(_JOBICY_URL, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for item in data.get("jobs", []):
        location = item.get("jobGeo", "Remote") or "Remote"
        jobs.append(_normalize(
            title=item.get("jobTitle", ""),
            company=item.get("companyName", ""),
            location=location,
            job_url=item.get("url", ""),
            description=item.get("jobDescription", "") or item.get("jobExcerpt", ""),
            date_posted=item.get("pubDate", ""),
            is_remote=True,
            site="jobicy",
        ))
    log.info("Jobicy: fetched %d jobs", len(jobs))
    return jobs


# ── Public interface ──────────────────────────────────────────────────────────

_SOURCES = [
    ("weworkremotely", _fetch_wwr),
    ("remotive", _fetch_remotive),
    ("jobicy", _fetch_jobicy),
]


def fetch_rss_jobs(profile: dict) -> list[dict]:
    """Fetch jobs from all RSS/API sources. Failures are logged and skipped."""
    all_jobs: list[dict] = []
    for name, fetcher in _SOURCES:
        try:
            jobs = fetcher()
            all_jobs.extend(jobs)
        except Exception as exc:
            log.warning("RSS source '%s' failed: %s", name, exc)
    log.info("RSS/API total: %d jobs from %d sources", len(all_jobs), len(_SOURCES))
    return all_jobs
