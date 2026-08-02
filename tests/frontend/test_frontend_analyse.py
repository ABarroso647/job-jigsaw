"""Regression tests for the "Analyse with AI" null-crash and its root cause.

Original bug (index.html runAnalyse): after rendering its chips, the handler ran
a document-wide ``querySelectorAll('.keyword-chip')`` and did
``input.checked = true`` on each match. Job-card badges reused ``.keyword-chip``
as input-less ``<span>``s, so once cards were rendered the loop reached a badge,
``querySelector('input')`` returned ``null``, and ``null.checked = true`` threw
"Cannot set properties of null (setting 'checked')" (surfaced as an alert; the
results panel never showed).

Two independent contracts are defended, one per root-cause half:
  1. Analyse only operates on the chips it renders — it must succeed regardless
     of any other ``.keyword-chip`` element on the page (loop-robustness fix).
  2. Job-card badges use their own ``.job-badge`` class, never the interactive
     ``.keyword-chip`` class (class-separation fix, so no future global
     ``.keyword-chip`` consumer can reach an input-less element).

Both use the frontend fixtures in tests/frontend/conftest.py and stub the
``/api/analyse`` LLM endpoint so they are deterministic and offline.
"""
import json

import pytest
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.playwright


ANALYSE_STUB = {
    "titles": ["Business Development Representative", "Account Executive"],
    "boost": [{"keyword": "HubSpot", "weight": 10}, {"keyword": "SaaS", "weight": 8}],
    "penalize": [{"keyword": "Salesforce", "weight": -20}],
}
_EXPECTED_CHIPS = 5  # 2 titles + 2 boost + 1 penalize


def test_analyse_ignores_foreign_input_less_keyword_chip(page: Page, live_app):
    """Analyse completes even when an input-less ``.keyword-chip`` exists elsewhere.

    Tests the crash invariant directly: the old loop swept every ``.keyword-chip``
    in the document and assumed a nested ``<input>``. Injecting one input-less
    ``.keyword-chip`` (the shape job-card badges — and any future markup — take)
    makes the old loop throw ``Cannot set properties of null``; the factory-based
    render only touches nodes it created, so it ignores the stray element.
    """
    page.route(
        "**/api/analyse",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(ANALYSE_STUB)
        ),
    )
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

    page.goto(live_app)
    # Inject a stray input-less `.keyword-chip` anywhere on the page — exactly the
    # shape the buggy loop choked on. The fixed Analyse must not touch it.
    page.evaluate(
        "() => { const s = document.createElement('span');"
        " s.className = 'keyword-chip'; s.textContent = 'stray';"
        " document.body.appendChild(s); }"
    )

    page.get_by_role("button", name="Analyse with AI").click()

    # Results render (the buggy loop threw before reaching this) ...
    expect(page.locator("#analyse-results")).to_be_visible()
    # ... every rendered chip has its input, checked by default (saveSelected
    # reads `#*-chips input:checked`, so default-checked is part of the contract).
    chip_inputs = page.locator("#analyse-results .keyword-chip input")
    expect(chip_inputs).to_have_count(_EXPECTED_CHIPS)
    assert chip_inputs.evaluate_all("els => els.length > 0 && els.every(e => e.checked)")
    assert dialogs == [], f"Analyse raised an error dialog: {dialogs}"


def test_job_badges_do_not_use_keyword_chip_class(page: Page, seeded_app):
    """Job-card badges use ``.job-badge`` and never the interactive chip class.

    Root-cause separation: the seeded unsent card has is_remote + job_type, so it
    renders two badges. They must be ``.job-badge`` (static) and must NOT carry
    ``.keyword-chip``, so no global ``.keyword-chip`` consumer can reach an
    input-less element and reintroduce the crash.
    """
    page.goto(seeded_app["url"])
    page.get_by_role("button", name="Preview").click()

    preview = page.locator("#preview-jobs")
    expect(preview.locator(".job-card")).to_have_count(1)
    # Two static badges rendered (Remote + job type) ...
    expect(preview.locator(".job-badge")).to_have_count(2)
    # ... and none of them wear the interactive chip class.
    expect(preview.locator(".keyword-chip")).to_have_count(0)
