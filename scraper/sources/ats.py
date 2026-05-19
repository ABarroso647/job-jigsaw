"""ATS direct scraping for Greenhouse, Lever, and Ashby."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

_TIMEOUT = 20


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


# ── Greenhouse ────────────────────────────────────────────────────────────────

def _fetch_greenhouse(company_name: str, slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        if resp.status_code == 404:
            log.warning("Greenhouse: no board found for slug '%s'", slug)
            return []
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.warning("Greenhouse request failed for '%s': %s", slug, exc)
        return []

    jobs = []
    for item in resp.json().get("jobs", []):
        # location field is a nested object on Greenhouse
        loc_obj = item.get("location", {})
        location = loc_obj.get("name", "") if isinstance(loc_obj, dict) else str(loc_obj)
        is_remote = "remote" in location.lower()
        description = item.get("content", "") or ""
        absolute_url = item.get("absolute_url", "") or item.get("job_url", "")
        jobs.append(_normalize(
            title=item.get("title", ""),
            company=company_name,
            location=location,
            job_url=absolute_url,
            description=description,
            date_posted=item.get("updated_at", ""),
            is_remote=is_remote,
            site="greenhouse",
        ))
    log.info("Greenhouse '%s': %d jobs", slug, len(jobs))
    return jobs


# ── Lever ─────────────────────────────────────────────────────────────────────

def _fetch_lever(company_name: str, slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        if resp.status_code == 404:
            log.warning("Lever: no board found for slug '%s'", slug)
            return []
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.warning("Lever request failed for '%s': %s", slug, exc)
        return []

    jobs = []
    for item in resp.json():
        categories = item.get("categories", {})
        location = categories.get("location", "") or item.get("country", "") or ""
        workplace = item.get("workplaceType", "")
        is_remote = workplace.lower() == "remote" if workplace else "remote" in location.lower()

        # Build description from available text fields
        desc_parts = []
        if item.get("descriptionPlain"):
            desc_parts.append(item["descriptionPlain"])
        elif item.get("description"):
            desc_parts.append(item["description"])
        for lst in item.get("lists", []):
            content = lst.get("content", "")
            if content:
                desc_parts.append(content)
        description = "\n".join(desc_parts)

        jobs.append(_normalize(
            title=item.get("text", ""),
            company=company_name,
            location=location,
            job_url=item.get("hostedUrl", ""),
            description=description,
            date_posted=None,
            is_remote=is_remote,
            site="lever",
        ))
    log.info("Lever '%s': %d jobs", slug, len(jobs))
    return jobs


# ── Ashby ─────────────────────────────────────────────────────────────────────

def _fetch_ashby(company_name: str, slug: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        if resp.status_code == 404:
            log.warning("Ashby: no board found for slug '%s'", slug)
            return []
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.warning("Ashby request failed for '%s': %s", slug, exc)
        return []

    jobs = []
    for item in resp.json().get("jobPostings", []):
        # Filter unlisted postings
        if not item.get("isListed", True):
            continue
        workplace = (item.get("workplaceType") or "").lower()
        is_remote = workplace == "remote"
        primary_loc = item.get("primaryLocation", {}) or {}
        location = primary_loc.get("city", "") or item.get("location", "") or ""
        if is_remote and not location:
            location = "Remote"
        description = item.get("descriptionPlain", "") or item.get("description", "") or ""
        jobs.append(_normalize(
            title=item.get("title", ""),
            company=company_name,
            location=location,
            job_url=item.get("postingUrl", "") or item.get("applicationUrl", ""),
            description=description,
            date_posted=item.get("publishedAt", ""),
            is_remote=is_remote,
            site="ashby",
        ))
    log.info("Ashby '%s': %d jobs", slug, len(jobs))
    return jobs


# ── Public interface ──────────────────────────────────────────────────────────

def fetch_ats_jobs(profile: dict) -> list[dict]:
    """Fetch jobs from ATS boards configured in profile.search.ats_companies."""
    companies = profile.get("search", {}).get("ats_companies", [])
    all_jobs: list[dict] = []

    for entry in companies:
        name = entry.get("name", "Unknown")

        if entry.get("greenhouse_slug"):
            try:
                all_jobs.extend(_fetch_greenhouse(name, entry["greenhouse_slug"]))
            except Exception as exc:
                log.warning("Greenhouse error for '%s': %s", name, exc)

        if entry.get("lever_slug"):
            try:
                all_jobs.extend(_fetch_lever(name, entry["lever_slug"]))
            except Exception as exc:
                log.warning("Lever error for '%s': %s", name, exc)

        if entry.get("ashby_slug"):
            try:
                all_jobs.extend(_fetch_ashby(name, entry["ashby_slug"]))
            except Exception as exc:
                log.warning("Ashby error for '%s': %s", name, exc)

    log.info("ATS total: %d jobs across %d companies", len(all_jobs), len(companies))
    return all_jobs
