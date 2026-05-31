"""Behavioral Playwright tests for the Job Jigsaw profile editor UI.

These cover the two behavioral cases the frontend baseline plan called for but
were never written:
  1. Rating a job updates the UI.
  2. The Preview tab loads jobs and excludes ones that were already sent.

They run against the function-scoped ``seeded_app`` fixture (tests/frontend/
conftest.py), which boots uvicorn against a temp DATA_DIR pre-seeded with known
jobs + a sent_jobs.db, so assertions are fully deterministic (no real network).
"""
import re

import pytest
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.playwright


def test_rating_a_job_updates_ui(page: Page, seeded_app):
    """Clicking the 'Interested' (like) control on a preview job updates the UI.

    rateJob() POSTs /api/jobs/rate then toggles the .liked class on the like
    button (main template renderJobs/rateJob). The like path (removeOnDislike for
    the dislike button only) keeps the card in place, so we can assert on the
    button's persistent active state. We also confirm the API call succeeded.
    """
    page.goto(seeded_app["url"])
    page.get_by_role("button", name="Preview").click()

    rateable = seeded_app["rateable"]
    card = page.locator(f'.job-card[data-url="{rateable["job_url"]}"]')
    expect(card).to_be_visible()

    like_btn = card.locator(".like-btn")
    # Not rated yet → no active class.
    expect(like_btn).not_to_have_class(re.compile(r"\bliked\b"))

    # Click and wait for the rate API round-trip so the assertion isn't racy.
    with page.expect_response(
        lambda r: "/api/jobs/rate" in r.url and r.request.method == "POST"
    ) as resp_info:
        like_btn.click()
    response = resp_info.value
    assert response.ok, f"rate API failed: {response.status}"

    # UI now reflects the rating: the like button is marked active.
    expect(like_btn).to_have_class(re.compile(r"\bliked\b"))


def test_preview_excludes_sent_jobs(page: Page, seeded_app):
    """Preview shows the unsent job and hides the one recorded in sent_jobs.db.

    query_jobs() (main.py:189-227) drops any job whose job_url is in
    sent_jobs.db's sent_jobs table, so the preview list should contain the unsent
    job's title and omit the sent one entirely.
    """
    page.goto(seeded_app["url"])
    page.get_by_role("button", name="Preview").click()

    unsent = seeded_app["unsent"]
    sent = seeded_app["sent"]

    jobs_container = page.locator("#preview-jobs")
    # Wait until preview has rendered at least one card (loadPreview is async).
    expect(jobs_container.locator(".job-card")).to_have_count(1)

    # The unsent job is present...
    unsent_card = jobs_container.locator(f'.job-card[data-url="{unsent["job_url"]}"]')
    expect(unsent_card).to_be_visible()
    expect(unsent_card).to_contain_text(unsent["title"])

    # ...and the already-sent job is excluded entirely.
    expect(jobs_container.locator(f'.job-card[data-url="{sent["job_url"]}"]')).to_have_count(0)
    expect(jobs_container).not_to_contain_text(sent["title"])
