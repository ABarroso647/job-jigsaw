"""Boolean search query generator for Job Jigsaw scraper."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)


def generate_search_term(profile: dict, settings) -> str:
    """Generate a Boolean search term from the candidate profile via LLM.

    Result is cached for 7 days in profile.search.generated_query.
    Falls back to a simple OR join of existing keywords on LLM failure.
    """
    cached = profile.get("search", {}).get("generated_query")
    cached_at = profile.get("search", {}).get("generated_query_at")
    if cached and cached_at:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached_at)).days
            if age < 7:
                log.info("Using cached generated query (age %d days)", age)
                return cached
        except Exception:
            pass

    keywords = profile.get("search", {}).get("keywords", [])
    description = profile.get("description", "")
    prompt = (
        "Generate a LinkedIn/Indeed Boolean job search string for this candidate.\n"
        f"Candidate description: {description}\n"
        f"Current keywords: {', '.join(keywords)}\n"
        "Rules: use OR between related titles, quote multi-word phrases, "
        "include seniority variants.\n"
        "Return ONLY the search string, nothing else. "
        'Example: "Account Executive" OR "AE" OR "Sales Executive" OR "BDR" OR "SDR"'
    )

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            json={
                "model": settings.openrouter_model,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.ok:
            query = resp.json()["choices"][0]["message"]["content"].strip().strip('"')
            log.info("Generated Boolean query: %s", query[:120])
            return query
        else:
            log.warning("LLM query gen failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("LLM query gen error: %s", exc)

    # Fallback: simple OR join of keywords
    fallback = " OR ".join(f'"{k}"' for k in keywords) if keywords else ""
    log.info("Using fallback keyword query")
    return fallback
