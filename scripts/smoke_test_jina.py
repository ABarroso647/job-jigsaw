#!/usr/bin/env python3
"""
Smoke test for Jina Reranker v3 integration.

Run with your real Jina API key to verify the payload shape, auth, and
response parsing all work before deploying to prod.

    JINA_API_KEY=jina_xxx python scripts/smoke_test_jina.py
"""
import json
import os
import sys

import requests

JINA_RERANK_URL = "https://api.jina.ai/v1/rerank"
JINA_RERANK_MODEL = "jina-reranker-v3"
JINA_TIMEOUT = 30

SAMPLE_RESUME = (
    "Experienced B2B sales professional in Toronto. 4 years closing SaaS deals, "
    "strong with HubSpot and Salesforce. Looking for Account Executive or AE roles "
    "at growth-stage tech companies. Prefer remote or hybrid."
)

SAMPLE_JOBS = [
    {
        "title": "Account Executive",
        "company": "CloudCo",
        "location": "Toronto, ON (Hybrid)",
        "job_url": "https://example.com/1",
        "score": 85,
        "reason": "Strong SaaS fit",
        "description": "Drive B2B SaaS sales, manage HubSpot CRM, close 6-figure deals.",
    },
    {
        "title": "Delivery Driver",
        "company": "LogisticsFast",
        "location": "Brampton, ON",
        "job_url": "https://example.com/2",
        "score": 82,
        "reason": "Weak fit — logistics",
        "description": "Drive cargo vans across the GTA. Valid G license required.",
    },
    {
        "title": "Business Development Rep",
        "company": "SaaSCorp",
        "location": "Toronto, ON (Remote)",
        "job_url": "https://example.com/3",
        "score": 80,
        "reason": "Good entry-level sales fit",
        "description": "Outbound prospecting, qualify leads, Salesforce pipeline management.",
    },
]


def build_documents(jobs):
    docs = []
    for job in jobs:
        desc = (job.get("description") or job.get("reason") or "")[:500]
        doc = f"{job['title']} at {job['company']} ({job['location']})\n{desc}"
        docs.append(doc)
    return docs


def main():
    api_key = os.environ.get("JINA_API_KEY", "").strip()
    if not api_key:
        print("ERROR: set JINA_API_KEY env var", file=sys.stderr)
        print("       Get a free key at https://jina.ai", file=sys.stderr)
        sys.exit(1)

    query = SAMPLE_RESUME
    documents = build_documents(SAMPLE_JOBS)

    print(f"Sending {len(SAMPLE_JOBS)} jobs to Jina Reranker v3...")
    print(f"Query (first 80 chars): {query[:80]}...")
    print()

    resp = requests.post(
        JINA_RERANK_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": JINA_RERANK_MODEL,
            "query": query,
            "documents": documents,
            "top_n": len(SAMPLE_JOBS),
        },
        timeout=JINA_TIMEOUT,
    )

    print(f"HTTP status: {resp.status_code}")
    if not resp.ok:
        print(f"ERROR: {resp.text}")
        sys.exit(1)

    data = resp.json()
    results = data.get("results", [])

    print(f"\nRe-ranked order (Jina score → original title):")
    for item in results:
        idx = item["index"]
        score = item["relevance_score"]
        job = SAMPLE_JOBS[idx]
        marker = " <-- expected #1 (sales match)" if job["title"] == "Account Executive" else ""
        print(f"  [{score:.4f}] {job['title']} at {job['company']}{marker}")

    # Sanity check: Delivery Driver should NOT be #1
    top_idx = results[0]["index"]
    top_job = SAMPLE_JOBS[top_idx]
    if top_job["title"] == "Delivery Driver":
        print("\nWARNING: Delivery Driver ranked #1 — re-ranker may not be working as expected.")
        sys.exit(1)
    else:
        print(f"\nPASS: '{top_job['title']}' ranked #1 (not the unrelated driver role).")

    # Check model field in response
    model_used = data.get("model", "")
    print(f"Model reported by API: {model_used or '(not in response)'}")

    print("\nSmoke test PASSED — Jina integration looks good.")


if __name__ == "__main__":
    main()
