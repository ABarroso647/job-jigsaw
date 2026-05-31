"""Playwright baseline tests for the Job Jigsaw profile editor UI."""
import pytest
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.playwright


@pytest.fixture(scope="session")
def base_url(live_app):
    return live_app


def test_page_title_present(page: Page, base_url: str):
    """Profile editor loads and has a page title."""
    page.goto(base_url)
    expect(page).to_have_title("Job Jigsaw")


def test_nav_tabs_exist(page: Page, base_url: str):
    """Resume, Settings, Preview, and History tabs exist in nav."""
    page.goto(base_url)
    nav = page.locator("nav")
    expect(nav.get_by_role("button", name="Resume")).to_be_visible()
    expect(nav.get_by_role("button", name="Settings")).to_be_visible()
    expect(nav.get_by_role("button", name="Preview")).to_be_visible()
    expect(nav.get_by_role("button", name="History")).to_be_visible()


def test_score_threshold_input_in_settings(page: Page, base_url: str):
    """Score threshold range input exists in the Settings tab."""
    page.goto(base_url)
    page.get_by_role("button", name="Settings").click()
    threshold = page.locator("#threshold")
    expect(threshold).to_be_visible()
    # Verify it is a range input for the score threshold
    assert threshold.get_attribute("type") == "range"


def test_history_tab_clear_unsent_button(page: Page, base_url: str):
    """History tab has a 'Clear unsent' button."""
    page.goto(base_url)
    page.get_by_role("button", name="History").click()
    clear_btn = page.get_by_role("button", name="Clear unsent")
    expect(clear_btn).to_be_visible()


def test_preview_tab_jobs_container(page: Page, base_url: str):
    """Preview tab has a jobs list container element."""
    page.goto(base_url)
    page.get_by_role("button", name="Preview").click()
    jobs_container = page.locator("#preview-jobs")
    expect(jobs_container).to_be_attached()
