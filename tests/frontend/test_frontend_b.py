"""Playwright frontend tests for Branch B — candidate wiki tab."""
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.playwright


@pytest.fixture(scope="session")
def base_url(live_app):
    return live_app


# ── B3: Wiki tab exists in nav ────────────────────────────────────────────────

def test_wiki_tab_in_nav(page: Page, base_url: str):
    """'Wiki' button exists in nav bar."""
    page.goto(base_url)
    wiki_btn = page.get_by_role("button", name="Wiki")
    expect(wiki_btn).to_be_visible()


# ── B3: Switching to wiki tab shows a textarea ────────────────────────────────

def test_wiki_tab_has_edit_area(page: Page, base_url: str):
    """Clicking Wiki tab reveals the edit textarea."""
    page.goto(base_url)
    page.get_by_role("button", name="Wiki").click()
    textarea = page.locator("#wiki-edit")
    expect(textarea).to_be_visible()


# ── B3: Regenerate button present ─────────────────────────────────────────────

def test_regenerate_button_present(page: Page, base_url: str):
    """'Regenerate Wiki' button exists in wiki tab."""
    page.goto(base_url)
    page.get_by_role("button", name="Wiki").click()
    regen_btn = page.get_by_role("button", name="Regenerate Wiki")
    expect(regen_btn).to_be_visible()


# ── B4: Resume health card present ───────────────────────────────────────────

def test_resume_health_card_present(page: Page, base_url: str):
    """Wiki tab contains a Resume Health card with an 'Analyze Now' button."""
    page.goto(base_url)
    page.get_by_role("button", name="Wiki").click()
    health_card = page.locator("#resume-health-card")
    expect(health_card).to_be_attached()
    analyze_btn = page.get_by_role("button", name="Analyze Now")
    expect(analyze_btn).to_be_visible()
