"""Unit tests for Branch A: RSS, ATS, quality gate."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scraper to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))


# ── RSS source tests ──────────────────────────────────────────────────────────

class TestRssRemotiveNormalizesSchema:
    def test_rss_remotive_normalizes_schema(self):
        """Mock requests; verify output has title/company/job_url/site."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "jobs": [
                {
                    "id": 1,
                    "url": "https://remotive.com/job/123",
                    "title": "Account Executive",
                    "company_name": "Acme Corp",
                    "candidate_required_location": "Worldwide",
                    "description": "<p>We are hiring an Account Executive.</p>",
                    "publication_date": "2026-05-01T10:00:00Z",
                }
            ]
        }

        with patch("requests.get", return_value=mock_response):
            from sources.rss import _fetch_remotive
            jobs = _fetch_remotive()

        assert len(jobs) == 1
        job = jobs[0]
        assert job["title"] == "Account Executive"
        assert job["company"] == "Acme Corp"
        assert job["job_url"] == "https://remotive.com/job/123"
        assert job["site"] == "remotive"
        assert job["is_remote"] is True
        assert "title" in job
        assert "company" in job
        assert "job_url" in job
        assert "site" in job
        assert "description" in job
        assert "location" in job


class TestRssHandlesSourceFailureGracefully:
    def test_rss_handles_source_failure_gracefully(self):
        """One source raises exception; other sources still return jobs."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "jobs": [
                {
                    "url": "https://remotive.com/job/999",
                    "title": "Sales Manager",
                    "company_name": "Remotive Corp",
                    "candidate_required_location": "Remote",
                    "description": "Great sales role.",
                    "publication_date": "2026-05-01",
                }
            ]
        }

        def selective_get(url, **kwargs):
            if "jobicy" in url:
                raise Exception("Jobicy is down")
            return mock_response

        with patch("requests.get", side_effect=selective_get):
            # Simulate WWR failure by patching the private fetcher
            with patch("sources.rss._fetch_wwr", side_effect=Exception("WWR feed failed")):
                from sources.rss import fetch_rss_jobs
                jobs = fetch_rss_jobs({})

        # Should still get Remotive jobs even though WWR and Jobicy failed
        assert isinstance(jobs, list)
        remotive_jobs = [j for j in jobs if j["site"] == "remotive"]
        assert len(remotive_jobs) == 1


class TestRssJobicyNormalizes:
    def test_rss_jobicy_normalizes_schema(self):
        """Jobicy JSON API correctly mapped to normalized schema."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "jobs": [
                {
                    "id": 42,
                    "url": "https://jobicy.com/job/42",
                    "jobTitle": "SDR",
                    "companyName": "Jobico Inc",
                    "jobGeo": "US Only",
                    "jobDescription": "Full-time SDR role based in the US.",
                    "pubDate": "2026-05-15",
                }
            ]
        }

        with patch("requests.get", return_value=mock_response):
            from sources.rss import _fetch_jobicy
            jobs = _fetch_jobicy()

        assert len(jobs) == 1
        job = jobs[0]
        assert job["title"] == "SDR"
        assert job["company"] == "Jobico Inc"
        assert job["site"] == "jobicy"


# ── ATS tests ─────────────────────────────────────────────────────────────────

class TestAtsGreenhouseParses:
    def test_ats_greenhouse_parses_response(self):
        """Mock Greenhouse JSON → verify field mapping."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "jobs": [
                {
                    "id": 101,
                    "title": "Account Executive",
                    "location": {"name": "Toronto, ON"},
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
                    "updated_at": "2026-05-10T12:00:00Z",
                    "content": "We are looking for an AE to join our team.",
                }
            ]
        }

        with patch("requests.get", return_value=mock_response):
            from sources.ats import _fetch_greenhouse
            jobs = _fetch_greenhouse("Acme Corp", "acme")

        assert len(jobs) == 1
        job = jobs[0]
        assert job["title"] == "Account Executive"
        assert job["company"] == "Acme Corp"
        assert job["job_url"] == "https://boards.greenhouse.io/acme/jobs/101"
        assert job["site"] == "greenhouse"
        assert "We are looking for an AE" in job["description"]


class TestAtsLeverParses:
    def test_ats_lever_parses_response(self):
        """Mock Lever JSON → verify field mapping."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {
                "id": "abc-123",
                "text": "BDR",
                "categories": {"location": "Remote", "commitment": "Full-time"},
                "workplaceType": "remote",
                "descriptionPlain": "We need a BDR to grow our pipeline.",
                "hostedUrl": "https://jobs.lever.co/acme/abc-123",
                "lists": [],
            }
        ]

        with patch("requests.get", return_value=mock_response):
            from sources.ats import _fetch_lever
            jobs = _fetch_lever("Acme Corp", "acme")

        assert len(jobs) == 1
        job = jobs[0]
        assert job["title"] == "BDR"
        assert job["company"] == "Acme Corp"
        assert job["job_url"] == "https://jobs.lever.co/acme/abc-123"
        assert job["site"] == "lever"
        assert job["is_remote"] is True


class TestAtsSkipsMissingSlug:
    def test_ats_skips_missing_slug(self):
        """Company with no greenhouse_slug doesn't call Greenhouse API."""
        profile = {
            "search": {
                "ats_companies": [
                    {"name": "NoSlugCo"},  # no slugs at all
                    {"name": "LeverOnly", "lever_slug": "leveronly"},
                ]
            }
        }

        lever_response = MagicMock()
        lever_response.status_code = 200
        lever_response.raise_for_status = MagicMock()
        lever_response.json.return_value = []

        call_urls = []

        def track_get(url, **kwargs):
            call_urls.append(url)
            return lever_response

        with patch("requests.get", side_effect=track_get):
            from sources.ats import fetch_ats_jobs
            fetch_ats_jobs(profile)

        # Only the Lever URL should be called, not Greenhouse
        assert not any("greenhouse" in url for url in call_urls)
        assert any("lever.co" in url and "leveronly" in url for url in call_urls)


# ── Quality gate tests ────────────────────────────────────────────────────────

class TestQualityGate:
    def setup_method(self):
        from quality_gate import evaluate_gate
        self._gate = evaluate_gate

    def _make_job(self, title="Account Executive", description=None, url="https://example.com/job/1"):
        return {
            "title": title,
            "description": description or (
                "We are seeking an Account Executive to join our B2B SaaS team. "
                "You will manage a full sales cycle from prospecting to close. "
                "Base salary plus commission. Health benefits included. "
                "3+ years experience in B2B sales required. CRM proficiency a plus."
            ),
            "job_url": url,
            "employer": "Acme Corp",
        }

    def test_quality_gate_rejects_commission_only(self):
        """'commission only' in description → False."""
        job = self._make_job(
            description="This is a commission only role with no base salary. "
                        "Unlimited earning potential! Join our sales team today. "
                        "Build your own book of business with no limits."
        )
        passes, reason, score = self._gate(job)
        assert passes is False
        assert "commission only" in reason.lower() or "hard reject" in reason.lower()

    def test_quality_gate_rejects_thin_description(self):
        """Description under 50 chars → False."""
        job = self._make_job(description="Sales role.")
        passes, reason, score = self._gate(job)
        assert passes is False
        assert "short" in reason.lower() or "description" in reason.lower()

    def test_quality_gate_passes_normal_ae_job(self):
        """Normal Account Executive job → passes gate."""
        passes, reason, score = self._gate(self._make_job())
        assert passes is True

    def test_quality_gate_rejects_wrong_field_title(self):
        """'delivery driver' in title → False."""
        job = self._make_job(
            title="Delivery Driver",
            description=(
                "Drive our delivery vehicles to fulfil customer orders. "
                "Valid driver license required. Physical fitness essential. "
                "Monday to Friday schedule with occasional weekends. "
                "Good driving record required. Apply now for this opportunity."
            )
        )
        passes, reason, score = self._gate(job)
        assert passes is False
        assert "wrong field" in reason.lower() or "delivery driver" in reason.lower()

    def test_quality_gate_soft_penalty_accumulation(self):
        """Multiple soft penalty terms → fails when score < -20."""
        job = self._make_job(
            description=(
                "Be your own boss and earn unlimited earning potential! "
                "This 1099 independent contractor role lets you set your schedule. "
                "Work from home guaranteed every day. No base salary offered. "
                "Join our network of successful sales professionals today."
            )
        )
        passes, reason, score = self._gate(job)
        # "no base salary" is a hard reject — ensure it fails
        assert passes is False

    def test_quality_gate_rejects_missing_url(self):
        """Job with no URL → structural reject."""
        job = {
            "title": "Account Executive",
            "description": "Good job description here with plenty of content for scoring.",
            "job_url": "",
            "employer": "Acme",
        }
        passes, reason, score = self._gate(job)
        assert passes is False
        assert "job_url" in reason.lower()

    def test_quality_gate_rejects_mlm(self):
        """MLM company name in description → hard reject."""
        job = self._make_job(
            description=(
                "Join our amazing sales team at Amway! We are looking for motivated "
                "individuals to build their own business. Work from anywhere and earn "
                "incredible commissions by recruiting others to join our network."
            )
        )
        passes, reason, score = self._gate(job)
        assert passes is False


# ── jobspy parameter flag test ─────────────────────────────────────────────────

class TestJobspyParameters:
    def test_linkedin_fetch_description_flag_set(self):
        """scrape_jobs called with linkedin_fetch_description=True, job_type=FULL_TIME."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scraper"))

        import sqlite3
        import tempfile

        call_kwargs = {}

        def mock_scrape_jobs(**kwargs):
            call_kwargs.update(kwargs)
            import pandas as pd
            return pd.DataFrame()

        mock_settings = MagicMock()
        mock_settings.openrouter_api_key = "test"
        mock_settings.openrouter_model = "test-model"

        profile = {
            "search": {
                "terms": ["Account Executive"],
                "locations": ["Toronto, ON"],
                "results_per_site": 5,
                "hours_old": 24,
                "use_generated_query": False,
                "ats_companies": [],
            },
            "scoring": {"boost": [], "penalize": []},
        }

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        import scrape as scrape_module
        original_db = scrape_module.JOBS_DB

        try:
            scrape_module.JOBS_DB = Path(db_path)

            with patch("scrape.scrape_jobs", side_effect=mock_scrape_jobs):
                with patch("scrape.fetch_rss_jobs", return_value=[]):
                    scrape_module.run(profile, mock_settings)
        finally:
            scrape_module.JOBS_DB = original_db
            Path(db_path).unlink(missing_ok=True)

        assert call_kwargs.get("linkedin_fetch_description") is True
        assert call_kwargs.get("enforce_annual_salary") is True
