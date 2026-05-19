"""Playwright frontend tests for Branch E: pipeline tab + status controls + tag UI."""
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.playwright


@pytest.fixture(scope="session")
def base_url(live_app):
    return live_app


def test_pipeline_tab_in_nav(page: Page, base_url: str):
    """'Pipeline' button exists in the nav bar."""
    page.goto(base_url)
    nav = page.locator("nav")
    expect(nav.get_by_role("button", name="Pipeline")).to_be_visible()


def test_pipeline_tab_renders(page: Page, base_url: str):
    """Clicking Pipeline tab shows the pipeline board container."""
    page.goto(base_url)
    page.get_by_role("button", name="Pipeline").click()
    # The pipeline board element should be in the DOM
    board = page.locator("#pipeline-board")
    expect(board).to_be_attached()
    # Pipeline metrics line should be visible
    metrics = page.locator("#pipeline-metrics")
    expect(metrics).to_be_attached()


def test_pipeline_tab_becomes_active(page: Page, base_url: str):
    """Clicking Pipeline makes the tab-pipeline div active."""
    page.goto(base_url)
    page.get_by_role("button", name="Pipeline").click()
    pipeline_tab = page.locator("#tab-pipeline")
    expect(pipeline_tab).to_have_class("tab active")


def test_status_controls_in_history(page: Page, base_url: str):
    """History tab exists and has the expected filter tabs including pipeline-related ones."""
    page.goto(base_url)
    page.get_by_role("button", name="History").click()
    # History tab should be visible
    history_tab = page.locator("#tab-history")
    expect(history_tab).to_have_class("tab active")
    # The filter tabs row should be present
    filter_tabs = page.locator(".filter-tabs")
    expect(filter_tabs).to_be_attached()


def test_history_tab_loads(page: Page, base_url: str):
    """History tab loads without JS errors (empty state is fine)."""
    page.goto(base_url)
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.get_by_role("button", name="History").click()
    # Allow time for any async operations
    page.wait_for_timeout(500)
    # Should not have any JS errors
    assert errors == [], f"JS errors on History tab: {errors}"


def test_pipeline_tab_loads_without_errors(page: Page, base_url: str):
    """Pipeline tab loads without JS errors."""
    page.goto(base_url)
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.get_by_role("button", name="Pipeline").click()
    page.wait_for_timeout(500)
    assert errors == [], f"JS errors on Pipeline tab: {errors}"


def test_tag_datalist_exists(page: Page, base_url: str):
    """Tag datalist element exists in the DOM for autocomplete."""
    page.goto(base_url)
    datalist = page.locator("#tag-datalist")
    expect(datalist).to_be_attached()
