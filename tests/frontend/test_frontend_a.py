"""Playwright frontend tests for Branch A: filtered jobs section in History tab."""
import pytest
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.playwright


@pytest.fixture(scope="session")
def base_url(live_app):
    return live_app


def test_filtered_section_visible_in_history(page: Page, base_url: str):
    """History tab has a 'Filtered' section with a collapsible card."""
    page.goto(base_url)
    page.get_by_role("button", name="History").click()
    # The Filtered card heading should be visible
    filtered_heading = page.locator("text=Filtered")
    expect(filtered_heading.first).to_be_visible()


def test_filtered_section_has_toggle(page: Page, base_url: str):
    """Filtered section is collapsible — clicking expands it."""
    page.goto(base_url)
    page.get_by_role("button", name="History").click()

    # The section content should be hidden initially
    filtered_section = page.locator("#filtered-jobs-section")
    # It starts hidden (display: none)
    assert filtered_section.is_hidden() or filtered_section.evaluate(
        "el => el.style.display === 'none'"
    )

    # Click the heading to expand
    page.locator("#filtered-chevron").click()

    # Now the section should be visible
    expect(page.locator("#filtered-jobs-list")).to_be_visible()


def test_filtered_badge_present(page: Page, base_url: str):
    """The Filtered heading area has a badge span element."""
    page.goto(base_url)
    page.get_by_role("button", name="History").click()
    badge = page.locator("#filtered-badge")
    expect(badge).to_be_attached()
