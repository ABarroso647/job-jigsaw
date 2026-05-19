"""Job Jigsaw — Profile Editor API"""

import json
import logging
import sqlite3
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
import mammoth
import pymupdf4llm
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from config import get_settings
from email_utils import build_email_html, send_email, send_plain_email
from telegram import send_message
from proposal import (
    INSIGHTS_HISTORY,
    apply_diff, append_history, build_history_entry,
    clear_proposal, load_history, load_proposal, revert_entry, save_proposal,
)

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
log = logging.getLogger(__name__)

PROFILE_PATH = Path("/data/profile.yaml")
JOBS_DB = Path("/data/jobs.db")
SENT_DB = Path("/data/sent_jobs.db")
INSIGHTS_META = Path("/data/insights_meta.json")

PAGE_SIZE = 25
INSIGHTS_AUTO_THRESHOLD = 10  # new ratings/notes since last run triggers auto-update
SCRAPER_URL = "http://scraper:3007"
SCRAPER_REQUEST_TIMEOUT = 5

SORT_MAP = {
    "score":      "suitability_score DESC, discovered_at DESC",
    "posted":     "date_posted DESC, discovered_at DESC",
    "discovered": "discovered_at DESC",
}


DEFAULT_PROFILE = {
    "resume": "",
    "description": "",
    "experience": [],       # list of {title, company, start, end, description, skills[]}
    "projects": [],         # list of {name, description, skills[]}
    "structured_skills": [],  # list of {name, category, evidence_level}
                              # evidence_level: "experience" | "project" | "skills_list"
    "wiki": "",
    "wiki_updated_at": None,
    "resume_health": None,
    "search": {
        "terms": [],
        "locations": ["Toronto, ON"],
        "hours_old": 24,
        "results_per_site": 25,
        "require_language": None,   # None = disabled; "en" = English only
        "allowed_regions": None,    # None = disabled; list of strings = whitelist
        "ats_companies": [],        # A3: [{name, greenhouse_slug?, lever_slug?, ashby_slug?}]
        "use_generated_query": False,  # A5: opt-in LLM-generated Boolean search query
    },
    "scoring": {
        "boost": [],
        "penalize": [],
    },
    "notification": {
        "score_threshold": 60,
        "max_jobs_per_email": 10,
        "timezone": "America/Toronto",
        "email_subject": "{count} new jobs today",
        "telegram_message": "Found {count} jobs (top: {top_score})",
        "max_job_age_days": 7,      # fallback age when date_posted is unknown
        "rerank_candidates": 30,    # how many to feed Jina before trimming to max_jobs
        "rerank_min_score": 0.3,    # drop jobs below this Jina relevance score
        "linkedin_max_jobs": 5,     # A6: per-source email budget
        "indeed_max_jobs": 3,
        "rss_max_jobs": 2,
        "ats_max_jobs": 3,
    },
}


# ── Startup initialization ────────────────────────────────────────────────────

def _init_profile() -> None:
    if not PROFILE_PATH.exists():
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        write_profile(DEFAULT_PROFILE)
        log.info("Created default profile.yaml at %s", PROFILE_PATH)


SEED_TAGS = [
    ("cold-calling-heavy", -0.8),
    ("remote-friendly", 0.8),
    ("too-junior", -0.6),
    ("too-senior", -0.4),
    ("wrong-industry", -0.7),
    ("strong-culture-fit", 0.7),
    ("good-compensation", 0.6),
]


def _init_jobs_db() -> None:
    JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(JOBS_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            employer TEXT,
            location TEXT,
            job_url TEXT UNIQUE,
            suitability_score INTEGER,
            suitability_reason TEXT,
            date_posted TEXT,
            is_remote INTEGER,
            job_type TEXT,
            discovered_at TEXT,
            user_rating INTEGER,
            notes TEXT,
            hidden INTEGER DEFAULT 0,
            is_applied INTEGER DEFAULT 0
        )
    """)
    # Migrations — idempotent
    for col_sql in [
        "ALTER TABLE jobs ADD COLUMN site TEXT",            # A3: job board source
        "ALTER TABLE jobs ADD COLUMN status TEXT",          # E1: application pipeline
        "ALTER TABLE jobs ADD COLUMN status_updated_at TEXT",
    ]:
        try:
            con.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    # E2 — dynamic feedback labels tables
    con.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            sentiment REAL DEFAULT 0,
            count INTEGER DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS job_tags (
            job_url TEXT NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (job_url, tag_id),
            FOREIGN KEY (tag_id) REFERENCES tags(id)
        )
    """)
    # A4 — filtered_jobs audit table
    con.execute("""
        CREATE TABLE IF NOT EXISTS filtered_jobs (
            job_url TEXT PRIMARY KEY,
            title TEXT,
            employer TEXT,
            location TEXT,
            site TEXT,
            reason TEXT,
            gate_score REAL,
            discovered_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Seed tags on first run (INSERT OR IGNORE is idempotent)
    for name, sentiment in SEED_TAGS:
        con.execute(
            "INSERT OR IGNORE INTO tags (name, sentiment) VALUES (?, ?)",
            (name, sentiment),
        )
    con.commit()
    con.close()


# ── Insights meta ─────────────────────────────────────────────────────────────

def _get_insights_meta() -> dict:
    if not INSIGHTS_META.exists():
        return {"last_run_at": None, "processed_count": 0}
    try:
        return json.loads(INSIGHTS_META.read_text())
    except Exception:
        return {"last_run_at": None, "processed_count": 0}


def _save_insights_meta(meta: dict) -> None:
    INSIGHTS_META.write_text(json.dumps(meta))


def _count_rated_noted() -> int:
    if not JOBS_DB.exists():
        return 0
    try:
        con = sqlite3.connect(JOBS_DB)
        row = con.execute("""
            SELECT COUNT(*) FROM jobs
            WHERE user_rating IS NOT NULL
               OR (notes IS NOT NULL AND notes != '')
        """).fetchone()
        con.close()
        return row[0] if row else 0
    except Exception:
        return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_profile()
    _init_jobs_db()
    yield


app = FastAPI(title="Job Jigsaw Profile Editor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Profile helpers ───────────────────────────────────────────────────────────

def read_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return yaml.safe_load(f)


def write_profile(data: dict) -> None:
    with open(PROFILE_PATH, "w") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _sent_map() -> dict[str, str]:
    """Returns {job_url: sent_at} for all sent jobs."""
    if not SENT_DB.exists():
        return {}
    try:
        con = sqlite3.connect(SENT_DB)
        result = {r[0]: r[1] for r in con.execute("SELECT job_url, sent_at FROM sent_jobs").fetchall()}
        con.close()
        return result
    except Exception:
        return {}


STALE_DATE_POSTED_DAYS = 14


def _get_all_tags() -> list[dict]:
    """Return all tags as a list of dicts."""
    if not JOBS_DB.exists():
        return []
    try:
        con = sqlite3.connect(JOBS_DB)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, name, sentiment, count FROM tags ORDER BY count DESC").fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _apply_tag_delta(jobs: list[dict]) -> list[dict]:
    """Adjust scores in-place by ±5 per tag (capped at ±15)."""
    if not jobs or not JOBS_DB.exists():
        return jobs
    try:
        con = sqlite3.connect(JOBS_DB)
        con.row_factory = sqlite3.Row
        for job in jobs:
            rows = con.execute("""
                SELECT t.sentiment FROM job_tags jt
                JOIN tags t ON t.id = jt.tag_id
                WHERE jt.job_url = ?
            """, (job["job_url"],)).fetchall()
            delta = sum(r["sentiment"] * 5 for r in rows)
            delta = max(-15, min(15, delta))
            job["score"] = job.get("score", 0) + delta
        con.close()
    except Exception as e:
        log.warning("tag_delta computation failed: %s", e)
    return jobs


def query_jobs(threshold: int, max_jobs: int, max_job_age_days: int = 7) -> list[dict]:
    if not JOBS_DB.exists():
        return []
    con = None
    try:
        con = sqlite3.connect(JOBS_DB)
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT title, employer, location, job_url, suitability_score,
                   date_posted, is_remote, job_type, user_rating, status
            FROM jobs
            WHERE suitability_score >= ?
              AND (user_rating IS NULL OR user_rating != -1)
              AND (hidden = 0 OR hidden IS NULL)
              AND (status IS NULL OR status = 'interested')
              AND (
                status = 'interested'
                OR (
                  (
                    date_posted IS NOT NULL AND date_posted != 'nan' AND date_posted != ''
                    AND date(date_posted) >= date('now', '-' || ? || ' days')
                  )
                  OR
                  (
                    (date_posted IS NULL OR date_posted = 'nan' OR date_posted = '')
                    AND discovered_at >= datetime('now', '-' || ? || ' days')
                  )
                )
              )
            ORDER BY suitability_score DESC
        """, (threshold, STALE_DATE_POSTED_DAYS, max_job_age_days)).fetchall()
        sent_urls = set(_sent_map().keys())
        results = []
        for r in rows:
            if r["job_url"] in sent_urls:
                continue
            job = dict(r)
            job["company"] = job.pop("employer", "")
            job["score"] = job.pop("suitability_score", 0)
            results.append(job)
            if len(results) >= max_jobs:
                break
        results = _apply_tag_delta(results)
        return results
    except Exception as e:
        log.error("jobs.db query failed: %s", e)
        return []
    finally:
        if con:
            con.close()


def _openrouter_call(settings, prompt: str, timeout: int = 120) -> str:
    """Call OpenRouter and return the raw content string (code fences stripped)."""
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.openrouter_model,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )
    if not resp.ok:
        raise HTTPException(502, f"OpenRouter error: {resp.status_code} {resp.text}")
    content = resp.json()["choices"][0]["message"]["content"]
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    return content


# ── Pydantic models ───────────────────────────────────────────────────────────

class AnalyseRequest(BaseModel):
    resume: str
    description: str


class AnalyseResponse(BaseModel):
    titles: list[str]
    boost: list[dict]
    penalize: list[dict]


class PreviewResponse(BaseModel):
    jobs: list[dict]
    count: int


class HistoryResponse(BaseModel):
    jobs: list[dict]
    total: int
    page: int
    pages: int


class TestSendResponse(BaseModel):
    ok: bool
    message: str


class RateRequest(BaseModel):
    job_url: str
    rating: int  # 1 = liked, -1 = disliked, 0 = reset


class NoteRequest(BaseModel):
    job_url: str
    notes: str


class HideRequest(BaseModel):
    job_url: str
    hidden: bool = True


class ResumeTipsResponse(BaseModel):
    tips: list[str]


class HistorySummarizeRequest(BaseModel):
    extra_prompt: str = ""


class ApplyRequest(BaseModel):
    job_url: str
    applied: bool


class FeedbackSummaryResponse(BaseModel):
    summary: str


class StatusUpdate(BaseModel):
    status: str  # interested | applied | interviewing | rejected | offer | null


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/profile")
def get_profile() -> dict:
    try:
        return read_profile()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/profile")
async def save_profile(request: Request):
    try:
        data = await request.json()
        write_profile(data)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/parse-resume")
async def parse_resume(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".pdf", ".docx", ".doc"}:
        raise HTTPException(400, "Only PDF and Word (.docx/.doc) files are supported")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        if suffix == ".pdf":
            text = pymupdf4llm.to_markdown(str(tmp_path))
        else:
            with open(tmp_path, "rb") as f:
                text = mammoth.convert_to_markdown(f).value
        return {"text": text}
    except Exception as e:
        raise HTTPException(500, f"Failed to parse file: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/api/analyse", response_model=AnalyseResponse)
def analyse(body: AnalyseRequest) -> AnalyseResponse:
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise HTTPException(400, "OPENROUTER_API_KEY not set")

    prompt = f"""You are a job search assistant. Based on the resume and description below, suggest:
1. 8-12 job title search terms to search on job boards (e.g. "Business Development Representative")
2. 15-20 boost keywords with weights (1-20): things that should appear in a GOOD job listing for this candidate — relevant industries, skills, tools, and role types they excel at
3. 5-10 penalize keywords with weights (-10 to -50): things that appear in job LISTINGS that would make the role a BAD fit — wrong industry, wrong seniority level, unrelated role types. Do NOT penalize based on anything in the candidate's own resume or background.

Return ONLY valid JSON:
{{
  "titles": ["Title 1", ...],
  "boost": [{{"keyword": "...", "weight": 15}}, ...],
  "penalize": [{{"keyword": "...", "weight": -30}}, ...]
}}

RESUME:
{body.resume}

WHAT THEY ARE LOOKING FOR:
{body.description}"""

    content = _openrouter_call(settings, prompt)
    try:
        return AnalyseResponse(**json.loads(content))
    except Exception as e:
        raise HTTPException(502, f"Model returned invalid JSON: {e}\n\nRaw: {content[:500]}")


@app.post("/api/resume-tips", response_model=ResumeTipsResponse)
def resume_tips(body: AnalyseRequest) -> ResumeTipsResponse:
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise HTTPException(400, "OPENROUTER_API_KEY not set")

    prompt = f"""You are a professional resume coach. Review this resume and job search goals.
Give 5-7 specific, actionable tips to improve the resume and job search strategy for the target roles.
Focus on concrete changes — wording, missing skills, formatting, or positioning issues.

Return ONLY valid JSON: {{"tips": ["Tip 1...", "Tip 2...", ...]}}

RESUME:
{body.resume}

TARGET ROLES & GOALS:
{body.description}"""

    content = _openrouter_call(settings, prompt)
    try:
        return ResumeTipsResponse(**json.loads(content))
    except Exception as e:
        raise HTTPException(502, f"Model returned invalid JSON: {e}\n\nRaw: {content[:500]}")


@app.get("/api/preview", response_model=PreviewResponse)
def preview() -> PreviewResponse:
    try:
        profile = read_profile()
        notif = profile["notification"]
        jobs = query_jobs(notif["score_threshold"], notif["max_jobs_per_email"],
                          notif.get("max_job_age_days", 7))
        return PreviewResponse(jobs=jobs, count=len(jobs))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/history/sent-dates")
def get_sent_dates():
    """Returns list of distinct sent dates (YYYY-MM-DD) newest first."""
    if not SENT_DB.exists():
        return []
    try:
        con = sqlite3.connect(SENT_DB)
        rows = con.execute("""
            SELECT DISTINCT DATE(sent_at) as d FROM sent_jobs ORDER BY d DESC
        """).fetchall()
        con.close()
        return [r[0] for r in rows]
    except Exception:
        return []


@app.get("/api/history", response_model=HistoryResponse)
def get_history(
    page: int = Query(1, ge=1),
    filter: str = Query("sent"),   # all | liked | disliked | sent | unsent
    sort: str = Query("score"),    # discovered | score | posted
    q: str = Query(""),
    sent_date: str = Query(""),    # YYYY-MM-DD, only applies when filter=sent
) -> HistoryResponse:
    if not JOBS_DB.exists():
        return HistoryResponse(jobs=[], total=0, page=1, pages=1)

    sort_clause = SORT_MAP.get(sort, SORT_MAP["discovered"])

    filter_sql = {
        "liked":        "AND user_rating = 1",
        "disliked":     "AND user_rating = -1",
        "applied":      "AND is_applied = 1",
        "interested":   "AND status = 'interested'",
        "interviewing": "AND status = 'interviewing'",
        "offer":        "AND status = 'offer'",
        "rejected":     "AND status = 'rejected'",
    }.get(filter, "")

    params: list = []
    search_sql = ""
    if q:
        search_sql = "AND (LOWER(title) LIKE ? OR LOWER(employer) LIKE ?)"
        like = f"%{q.lower()}%"
        params = [like, like]

    con = None
    try:
        con = sqlite3.connect(JOBS_DB)
        con.row_factory = sqlite3.Row
        rows = con.execute(f"""
            SELECT id, title, employer, location, job_url,
                   suitability_score, suitability_reason,
                   date_posted, is_remote, job_type,
                   discovered_at, user_rating, notes, is_applied,
                   status, status_updated_at
            FROM jobs
            WHERE (hidden = 0 OR hidden IS NULL)
            {filter_sql}
            {search_sql}
            ORDER BY {sort_clause}
        """, params).fetchall()
    except Exception as e:
        log.error("history query failed: %s", e)
        raise HTTPException(500, str(e))
    finally:
        if con:
            con.close()

    sent = _sent_map()
    jobs: list[dict] = []
    for r in rows:
        job = dict(r)
        job["company"] = job.pop("employer", "")
        job["score"] = job.pop("suitability_score", 0)
        job["reason"] = job.pop("suitability_reason", "") or ""
        job["sent_at"] = sent.get(job["job_url"])
        job["sent"] = job["sent_at"] is not None
        jobs.append(job)

    if filter == "sent":
        jobs = [j for j in jobs if j["sent"]]
        if sent_date:
            jobs = [j for j in jobs if j.get("sent_at", "").startswith(sent_date)]
    elif filter == "unsent":
        jobs = [j for j in jobs if not j["sent"]]
    elif filter in ("applied", "interested", "interviewing", "offer", "rejected"):
        pass  # already filtered in SQL

    total = len(jobs)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE

    return HistoryResponse(
        jobs=jobs[start:start + PAGE_SIZE],
        total=total,
        page=page,
        pages=pages,
    )


@app.post("/api/jobs/rate")
def rate_job(body: RateRequest):
    if body.rating not in (-1, 0, 1):
        raise HTTPException(400, "rating must be -1, 0, or 1")
    if not JOBS_DB.exists():
        raise HTTPException(404, "No jobs database")
    try:
        con = sqlite3.connect(JOBS_DB)
        con.execute("UPDATE jobs SET user_rating = ? WHERE job_url = ?",
                    (None if body.rating == 0 else body.rating, body.job_url))
        con.commit()
        con.close()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/jobs/note")
def save_note(body: NoteRequest):
    if not JOBS_DB.exists():
        raise HTTPException(404, "No jobs database")
    try:
        con = sqlite3.connect(JOBS_DB)
        con.execute("UPDATE jobs SET notes = ? WHERE job_url = ?",
                    (body.notes or None, body.job_url))
        con.commit()
        con.close()
        # Async tag extraction from note
        try:
            settings = get_settings()
            if settings.openrouter_api_key and body.notes:
                import threading
                def _bg_extract():
                    tags = extract_tags_from_note(body.notes, body.job_url, settings)
                    if tags:
                        _store_tags_for_job(body.job_url, tags)
                threading.Thread(target=_bg_extract, daemon=True).start()
        except Exception as e:
            log.warning("Tag extraction setup failed: %s", e)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/jobs/apply")
def apply_job(body: ApplyRequest):
    if not JOBS_DB.exists():
        raise HTTPException(404, "No jobs database")
    try:
        con = sqlite3.connect(JOBS_DB)
        con.execute("UPDATE jobs SET is_applied = ? WHERE job_url = ?",
                    (1 if body.applied else 0, body.job_url))
        con.commit()
        con.close()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/jobs/hide")
def hide_job(body: HideRequest):
    if not JOBS_DB.exists():
        raise HTTPException(404, "No jobs database")
    try:
        con = sqlite3.connect(JOBS_DB)
        con.execute("UPDATE jobs SET hidden = ? WHERE job_url = ?",
                    (1 if body.hidden else 0, body.job_url))
        con.commit()
        con.close()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── E1: Application pipeline ──────────────────────────────────────────────────

@app.put("/api/jobs/{job_url:path}/status")
def update_job_status(job_url: str, body: StatusUpdate):
    valid = {None, "interested", "applied", "interviewing", "rejected", "offer"}
    status_val = body.status if body.status and body.status.lower() != "null" else None
    if status_val not in valid:
        raise HTTPException(400, f"Invalid status: {body.status}")
    if not JOBS_DB.exists():
        raise HTTPException(404, "No jobs database")
    try:
        con = sqlite3.connect(JOBS_DB)
        is_applied = 1 if status_val in ("applied", "interviewing", "offer") else 0
        con.execute(
            "UPDATE jobs SET status=?, status_updated_at=?, is_applied=? WHERE job_url=?",
            (status_val, datetime.now(timezone.utc).isoformat(), is_applied, job_url),
        )
        con.commit()
        con.close()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/pipeline/stats")
def pipeline_stats():
    if not JOBS_DB.exists():
        return {}
    try:
        con = sqlite3.connect(JOBS_DB)
        rows = con.execute("""
            SELECT status, COUNT(*) as n,
                   SUM(CASE WHEN date(status_updated_at) >= date('now', '-7 days') THEN 1 ELSE 0 END) as this_week
            FROM jobs
            WHERE status IS NOT NULL
            GROUP BY status
        """).fetchall()
        con.close()
        return {r[0]: {"total": r[1], "this_week": r[2]} for r in rows}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/jobs")
def list_jobs(include_all_status: bool = Query(False)):
    """Return jobs for the pipeline view, optionally including all status values."""
    if not JOBS_DB.exists():
        return []
    try:
        con = sqlite3.connect(JOBS_DB)
        con.row_factory = sqlite3.Row
        if include_all_status:
            rows = con.execute("""
                SELECT id, title, employer, location, job_url, suitability_score,
                       date_posted, is_remote, job_type, user_rating, status, status_updated_at
                FROM jobs
                WHERE (hidden = 0 OR hidden IS NULL)
                  AND status IS NOT NULL
                ORDER BY status_updated_at DESC
            """).fetchall()
        else:
            rows = con.execute("""
                SELECT id, title, employer, location, job_url, suitability_score,
                       date_posted, is_remote, job_type, user_rating, status, status_updated_at
                FROM jobs
                WHERE (hidden = 0 OR hidden IS NULL)
                ORDER BY suitability_score DESC
                LIMIT 100
            """).fetchall()
        con.close()
        result = []
        for r in rows:
            job = dict(r)
            job["company"] = job.pop("employer", "")
            job["score"] = job.pop("suitability_score", 0)
            result.append(job)
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


# ── E2: Dynamic feedback labels ───────────────────────────────────────────────

def _deduplicate_tag(name: str, existing: list[str]) -> str:
    """Use edit distance to find if tag already exists under a similar name."""
    try:
        from rapidfuzz import fuzz
        for existing_name in existing:
            if fuzz.ratio(name, existing_name) > 80:
                return existing_name
    except ImportError:
        pass  # rapidfuzz optional
    return name


def extract_tags_from_note(note: str, job_url: str, settings) -> list[str]:
    """Extract 0-3 tags from a note using cheapest LLM."""
    if not note or len(note.strip()) < 10:
        return []
    existing_tags = _get_all_tags()
    existing_names = [t["name"] for t in existing_tags]
    prompt = f"""Extract 0-3 short labels (tags) from this job note.
Note: "{note}"
Existing tags (prefer these if they match): {existing_names}
Rules: tags are 1-4 words, lowercase, hyphenated. Return only a JSON array of strings. Example: ["cold-calling-heavy", "no-base-salary"]
If the note doesn't warrant any tags, return []."""

    try:
        resp = _openrouter_call(settings, prompt, timeout=15)
        tags = json.loads(resp)
        return [t.lower().replace(" ", "-") for t in tags if isinstance(t, str)][:3]
    except Exception:
        return []


def _store_tags_for_job(job_url: str, tag_names: list[str]) -> None:
    """Store extracted tags in DB with deduplication."""
    if not tag_names or not JOBS_DB.exists():
        return
    try:
        con = sqlite3.connect(JOBS_DB)
        existing_names = [r[0] for r in con.execute("SELECT name FROM tags").fetchall()]
        for raw_name in tag_names:
            name = _deduplicate_tag(raw_name, existing_names)
            con.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
            tag_id = con.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0]
            con.execute("INSERT OR IGNORE INTO job_tags (job_url, tag_id) VALUES (?,?)", (job_url, tag_id))
            con.execute("UPDATE tags SET count = count + 1 WHERE id=? AND NOT EXISTS "
                        "(SELECT 1 FROM job_tags WHERE job_url=? AND tag_id=?)",
                        (tag_id, job_url, tag_id))
            if name not in existing_names:
                existing_names.append(name)
        con.commit()
        con.close()
    except Exception as e:
        log.warning("Failed to store tags: %s", e)


@app.get("/api/tags")
def get_tags():
    """Get all tags with counts."""
    if not JOBS_DB.exists():
        return []
    con = sqlite3.connect(JOBS_DB)
    con.row_factory = sqlite3.Row
    tags = con.execute("SELECT * FROM tags ORDER BY count DESC").fetchall()
    con.close()
    return [dict(t) for t in tags]


@app.post("/api/jobs/{job_url:path}/tags")
def add_job_tag(job_url: str, body: dict):
    """Add a tag to a job."""
    tag_name = body.get("name", "").lower().strip()
    if not tag_name:
        raise HTTPException(400, "tag name required")
    if not JOBS_DB.exists():
        raise HTTPException(404, "No jobs database")
    con = sqlite3.connect(JOBS_DB)
    con.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
    tag_id = con.execute("SELECT id FROM tags WHERE name=?", (tag_name,)).fetchone()[0]
    already = con.execute("SELECT 1 FROM job_tags WHERE job_url=? AND tag_id=?", (job_url, tag_id)).fetchone()
    con.execute("INSERT OR IGNORE INTO job_tags (job_url, tag_id) VALUES (?,?)", (job_url, tag_id))
    if not already:
        con.execute("UPDATE tags SET count = count + 1 WHERE id=?", (tag_id,))
    con.commit()
    con.close()
    return {"ok": True}


@app.delete("/api/jobs/{job_url:path}/tags/{tag_name}")
def remove_job_tag(job_url: str, tag_name: str):
    if not JOBS_DB.exists():
        raise HTTPException(404, "No jobs database")
    con = sqlite3.connect(JOBS_DB)
    tag = con.execute("SELECT id FROM tags WHERE name=?", (tag_name,)).fetchone()
    if tag:
        deleted = con.execute("DELETE FROM job_tags WHERE job_url=? AND tag_id=?", (job_url, tag[0])).rowcount
        if deleted:
            con.execute("UPDATE tags SET count = MAX(0, count - 1) WHERE id=?", (tag[0],))
    con.commit()
    con.close()
    return {"ok": True}


@app.get("/api/jobs/{job_url:path}/tags")
def get_job_tags(job_url: str):
    """Get tags for a specific job."""
    if not JOBS_DB.exists():
        return []
    con = sqlite3.connect(JOBS_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT t.name, t.sentiment FROM job_tags jt
        JOIN tags t ON t.id = jt.tag_id
        WHERE jt.job_url = ?
    """, (job_url,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.delete("/api/jobs/clear-unsent")
def clear_unsent_jobs():
    if not JOBS_DB.exists():
        return {"deleted": 0}
    try:
        con = sqlite3.connect(JOBS_DB)
        if SENT_DB.exists():
            sent_con = sqlite3.connect(SENT_DB)
            sent_urls = {r[0] for r in sent_con.execute("SELECT job_url FROM sent_jobs").fetchall()}
            sent_con.close()
        else:
            sent_urls = set()
        if sent_urls:
            placeholders = ",".join("?" * len(sent_urls))
            cur = con.execute(
                f"DELETE FROM jobs WHERE job_url NOT IN ({placeholders})",
                list(sent_urls),
            )
        else:
            cur = con.execute("DELETE FROM jobs")
        deleted = cur.rowcount
        con.commit()
        con.close()
        return {"deleted": deleted}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/jobs/filtered")
def get_filtered_jobs():
    """Return last 50 gate-rejected jobs with rejection reason (A4 audit trail)."""
    if not JOBS_DB.exists():
        return []
    try:
        con = sqlite3.connect(JOBS_DB)
        con.row_factory = sqlite3.Row
        # filtered_jobs table may not exist in older DBs — create if missing
        con.execute("""
            CREATE TABLE IF NOT EXISTS filtered_jobs (
                job_url TEXT PRIMARY KEY,
                title TEXT,
                employer TEXT,
                location TEXT,
                site TEXT,
                reason TEXT,
                gate_score REAL,
                discovered_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        rows = con.execute("""
            SELECT job_url, title, employer, location, site, reason, gate_score, discovered_at
            FROM filtered_jobs
            ORDER BY discovered_at DESC
            LIMIT 50
        """).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/insights-status")
def insights_status():
    meta = _get_insights_meta()
    current = _count_rated_noted()
    new_since = max(0, current - meta.get("processed_count", 0))
    return {
        "last_run_at": meta.get("last_run_at"),
        "new_since_last_run": new_since,
        "threshold": INSIGHTS_AUTO_THRESHOLD,
    }


def _build_insights_prompt(profile: dict, sections: list[str], extra_prompt: str = "") -> str:
    scoring = profile.get("scoring", {})
    existing_boost    = [k["keyword"] for k in scoring.get("boost", [])]
    existing_penalize = [k["keyword"] for k in scoring.get("penalize", [])]
    existing_terms    = profile.get("search", {}).get("terms", [])
    extra = f"\nADDITIONAL CONTEXT FROM USER: {extra_prompt}" if extra_prompt.strip() else ""
    return f"""You are a job search optimizer. Analyze the user's liked/disliked/noted job listings
and propose changes to their search profile by adding new boost keywords, penalize keywords,
and search terms that reflect their preferences.

CURRENT PROFILE:
- Boost keywords: {', '.join(existing_boost) or 'none'}
- Penalize keywords: {', '.join(existing_penalize) or 'none'}
- Search terms: {', '.join(existing_terms) or 'none'}
{extra}

Only suggest NEW items not already in the current profile. Be conservative — 1-5 items per category max.

Return ONLY valid JSON:
{{
  "boost_add": [{{"keyword": "...", "weight": 10}}, ...],
  "penalize_add": [{{"keyword": "...", "weight": -20}}, ...],
  "terms_add": ["...", ...],
  "summary": "1-2 sentence description of what would be updated and why"
}}

{chr(10).join(sections)}"""


def _collect_feedback_rows() -> list:
    if not JOBS_DB.exists():
        return []
    con = sqlite3.connect(JOBS_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT title, employer, location, job_type, is_remote,
               suitability_score, user_rating, notes
        FROM jobs
        WHERE hidden = 0
          AND (user_rating IS NOT NULL OR (notes IS NOT NULL AND notes != ''))
        ORDER BY discovered_at DESC
        LIMIT 200
    """).fetchall()
    con.close()
    return rows


def _rows_to_sections(rows) -> list[str]:
    liked, disliked, noted = [], [], []
    for r in rows:
        line = f'- "{r["title"]}" at {r["employer"]} — score {r["suitability_score"]}'
        if r["is_remote"]:
            line += ", Remote"
        if r["job_type"]:
            line += f", {r['job_type']}"
        if r["notes"]:
            line += f'\n  Note: "{r["notes"]}"'
        if r["user_rating"] == 1:
            liked.append(line)
        elif r["user_rating"] == -1:
            disliked.append(line)
        else:
            noted.append(line)
    sections = []
    if liked:     sections.append("LIKED JOBS:\n"          + "\n".join(liked[:50]))
    if disliked:  sections.append("DISLIKED JOBS:\n"       + "\n".join(disliked[:50]))
    if noted:     sections.append("NOTED JOBS (no rating):\n" + "\n".join(noted[:30]))
    return sections


def _generate_proposal(settings, profile: dict, extra_prompt: str = "") -> dict:
    """Call the model and return a raw proposal dict (not yet saved or applied)."""
    rows = _collect_feedback_rows()
    if not rows:
        return {"ok": False, "message": "No rated or noted jobs yet."}

    sections = _rows_to_sections(rows)
    prompt = _build_insights_prompt(profile, sections, extra_prompt)

    content = _openrouter_call(settings, prompt, timeout=60)

    try:
        result = json.loads(content)
    except Exception as e:
        log.warning("Insights JSON parse failed: %s | raw: %.200s", e, content)
        return {"ok": False, "message": f"Model returned invalid JSON: {e}"}

    return {
        "ok": True,
        "boost_add":    result.get("boost_add", []),
        "penalize_add": result.get("penalize_add", []),
        "terms_add":    result.get("terms_add", []),
        "summary":      result.get("summary", ""),
        "conversation": [],
    }


def _generate_feedback_summary(rows, settings) -> str:
    """Build a natural-language feedback summary from rated/noted job rows."""
    if not rows:
        return ""
    lines = []
    for r in rows:
        rating_map = {1: "liked", -1: "disliked"}
        rating_str = rating_map.get(r["user_rating"], "noted")
        line = f'- {rating_str}: "{r["title"]}" at {r["employer"]}'
        if r["notes"]:
            line += f' — Note: "{r["notes"]}"'
        lines.append(line)

    prompt = (
        "Based on these job ratings and notes, summarize in 2-3 sentences what this "
        "candidate likes and dislikes about job postings. Be specific about patterns "
        "(company types, role types, requirements, industries).\n\n"
        f"FEEDBACK:\n{chr(10).join(lines)}\n\n"
        'Return ONLY valid JSON: {"summary": "<2-3 sentences>"}'
    )
    content = _openrouter_call(settings, prompt, timeout=60)
    try:
        return json.loads(content).get("summary", "")
    except Exception:
        return content[:500]


def _proposal_email_body(proposal: dict) -> str:
    boost    = [i["keyword"] for i in proposal.get("boost_add", [])]
    penalize = [i["keyword"] for i in proposal.get("penalize_add", [])]
    terms    = proposal.get("terms_add", [])
    lines = [proposal.get("summary", ""), ""]
    if boost:    lines.append(f"Boost keywords to add: {', '.join(boost)}")
    if penalize: lines.append(f"Penalize keywords to add: {', '.join(penalize)}")
    if terms:    lines.append(f"Search terms to add: {', '.join(terms)}")
    if not (boost or penalize or terms):
        lines.append("(No changes proposed)")
    lines += ["", "Reply APPROVE to apply, REJECT to discard, or tell me what to change."]
    return "\n".join(lines)


@app.post("/api/profile/refresh-feedback", response_model=FeedbackSummaryResponse)
def refresh_feedback_summary() -> FeedbackSummaryResponse:
    """Generate a feedback_summary from recent ratings/notes and store it in profile.yaml."""
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise HTTPException(400, "OPENROUTER_API_KEY not set")

    rows = []
    if JOBS_DB.exists():
        try:
            con = sqlite3.connect(JOBS_DB)
            con.row_factory = sqlite3.Row
            rows = con.execute("""
                SELECT title, employer, user_rating, notes
                FROM jobs
                WHERE discovered_at >= datetime('now', '-90 days')
                  AND (user_rating IS NOT NULL OR (notes IS NOT NULL AND notes != ''))
                ORDER BY discovered_at DESC
                LIMIT 100
            """).fetchall()
            con.close()
        except Exception as e:
            log.error("jobs.db feedback query failed: %s", e)

    summary = _generate_feedback_summary(rows, settings)

    try:
        profile = read_profile()
        profile["feedback_summary"] = summary
        profile["feedback_summary_updated_at"] = datetime.now(timezone.utc).isoformat()
        write_profile(profile)
    except Exception as e:
        raise HTTPException(500, str(e))

    return FeedbackSummaryResponse(summary=summary)


@app.post("/api/history/summarize")
def history_summarize(body: HistorySummarizeRequest):
    """Generate a proposal, store it, send notification email, return proposal for UI."""
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise HTTPException(400, "OPENROUTER_API_KEY not set")
    try:
        profile = read_profile()
    except Exception as e:
        raise HTTPException(500, str(e))

    result = _generate_proposal(settings, profile, extra_prompt=body.extra_prompt)
    if not result["ok"]:
        raise HTTPException(400, result["message"])

    # Update meta so the reminder counter resets
    meta = _get_insights_meta()
    meta["last_run_at"] = datetime.now(timezone.utc).isoformat()
    meta["processed_count"] = _count_rated_noted()
    _save_insights_meta(meta)

    # Send email so user can also approve/reject via reply
    try:
        msg_id, _ = send_plain_email(
            settings,
            subject="Job Jigsaw — Profile update proposal",
            body=_proposal_email_body(result),
        )
        result["message_id"] = msg_id
    except Exception as e:
        log.warning("Could not send proposal email: %s", e)
        result["message_id"] = ""

    result["status"] = "pending"
    result["proposed_at"] = datetime.now(timezone.utc).isoformat()
    save_proposal(result)

    return result


@app.post("/api/insights/approve")
def insights_approve():
    proposal = load_proposal()
    if not proposal:
        raise HTTPException(404, "No active proposal")
    try:
        profile = read_profile()
    except Exception as e:
        raise HTTPException(500, str(e))

    applied = apply_diff(profile, proposal)
    write_profile(profile)
    entry = build_history_entry(proposal, applied)
    append_history(entry)
    clear_proposal()

    counts = []
    if applied["boost"]:    counts.append(f"{len(applied['boost'])} boost keyword(s)")
    if applied["penalize"]: counts.append(f"{len(applied['penalize'])} penalize keyword(s)")
    if applied["terms"]:    counts.append(f"{len(applied['terms'])} search term(s)")
    change_str = ", ".join(counts) if counts else "no new items"

    # Reply email if we have a message_id
    if proposal.get("message_id"):
        try:
            settings = get_settings()
            send_plain_email(
                settings,
                subject="Re: Job Jigsaw — Profile update proposal",
                body=f"Done! Applied: {change_str}.\n\n{proposal.get('summary', '')}",
                in_reply_to=proposal["message_id"],
            )
        except Exception as e:
            log.warning("Approval reply email failed: %s", e)

    return {"ok": True, "message": f"Applied: {change_str}."}


@app.post("/api/insights/reject")
def insights_reject():
    proposal = load_proposal()
    if not proposal:
        raise HTTPException(404, "No active proposal")

    if proposal.get("message_id"):
        try:
            settings = get_settings()
            send_plain_email(
                settings,
                subject="Re: Job Jigsaw — Profile update proposal",
                body="Got it — no changes made.",
                in_reply_to=proposal["message_id"],
            )
        except Exception as e:
            log.warning("Rejection reply email failed: %s", e)

    clear_proposal()
    return {"ok": True, "message": "Proposal rejected — no changes made."}


@app.get("/api/insights/proposal")
def get_proposal():
    p = load_proposal()
    return p or {}


@app.get("/api/insights/history")
def get_insights_history():
    return load_history()


@app.post("/api/insights/revert/{index}")
def revert_insight(index: int):
    history = load_history()
    if index < 0 or index >= len(history):
        raise HTTPException(404, "History entry not found")
    entry = history[index]
    if entry.get("reverted"):
        raise HTTPException(400, "Already reverted")
    try:
        profile = read_profile()
    except Exception as e:
        raise HTTPException(500, str(e))
    removed = revert_entry(profile, entry)
    write_profile(profile)
    history[index]["reverted"] = True
    INSIGHTS_HISTORY.write_text(json.dumps(history, indent=2))
    return {"ok": True, "message": f"Reverted — removed {removed} item(s) from profile."}

@app.post("/api/profile/migrate-from-resume")
def migrate_from_resume():
    """One-time LLM call: extract structured experience/projects/skills from resume text blob."""
    profile = read_profile()
    resume_text = profile.get("resume", "")
    if not resume_text:
        raise HTTPException(400, "No resume text found")
    settings = get_settings()
    prompt = (
        "Extract structured data from this sales professional's resume.\n"
        "Return a JSON object with:\n"
        "- experience: array of {title, company, start (YYYY-MM), end (YYYY-MM or \'present\'), description, skills: []}\n"
        "- projects: array of {name, description, skills: []}\n"
        "- structured_skills: array of {name, category (crm|methodology|industry|motion|segment|general), "
        "evidence_level (experience|project|skills_list)}\n\n"
        f"Resume:\n{resume_text}\n\nReturn only valid JSON, no explanation."
    )
    resp = _openrouter_call(settings, prompt, timeout=60)
    try:
        data = json.loads(resp)
        profile["experience"] = data.get("experience", [])
        profile["projects"] = data.get("projects", [])
        profile["structured_skills"] = data.get("structured_skills", [])
        write_profile(profile)
        return {
            "migrated": True,
            "experience_count": len(profile["experience"]),
            "skills_count": len(profile["structured_skills"]),
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to parse LLM response: {e}")


@app.post("/api/profile/regenerate-wiki")
def regenerate_wiki():
    """Generate candidate wiki from structured profile fields."""
    profile = read_profile()
    settings = get_settings()

    exp_entries = profile.get("experience", [])
    if exp_entries:
        exp_text = "\n".join(
            f"- {e['title']} at {e['company']} ({e.get('start', '')}\u2013{e.get('end', '')}): {e.get('description', '')}"
            for e in exp_entries
        )
    else:
        exp_text = profile.get("resume", "")[:2000]

    prompt = (
        "Create a structured candidate wiki in markdown for an LLM to use as context when evaluating job fit.\n\n"
        f"Candidate info:\n{exp_text}\n\n"
        f"Projects: {profile.get('projects', [])}\n"
        f"Skills: {profile.get('structured_skills', [])}\n"
        f"Description: {profile.get('description', '')}\n\n"
        "Generate a wiki with exactly these 8 sections:\n"
        "# Candidate Wiki\n"
        "## Identity & Target Role\n"
        "## Quota & Revenue Performance\n"
        "## CRM & Tools\n"
        "## Work Experience (chronological)\n"
        "## Sales Skills & Methodologies\n"
        "## Client & Deal Types\n"
        "## Industry Knowledge\n"
        "## What This Candidate Is NOT\n\n"
        "Be specific. Include real numbers, real company names, real tools. "
        "The \'What This Candidate Is NOT\' section should list roles, industries, and contexts "
        "that don\'t fit this candidate \u2014 this is the most important section for avoiding false positives.\n"
        "Keep total length under 800 tokens."
    )

    wiki = _openrouter_call(settings, prompt, timeout=60)
    profile["wiki"] = wiki
    profile["wiki_updated_at"] = datetime.now(timezone.utc).isoformat()
    write_profile(profile)
    return {"wiki": wiki, "updated_at": profile["wiki_updated_at"]}


@app.get("/api/profile/wiki")
def get_wiki():
    profile = read_profile()
    return {"wiki": profile.get("wiki", ""), "updated_at": profile.get("wiki_updated_at")}


@app.put("/api/profile/wiki")
async def save_wiki(request: Request):
    body = await request.json()
    profile = read_profile()
    profile["wiki"] = body.get("wiki", "")
    profile["wiki_updated_at"] = datetime.now(timezone.utc).isoformat()
    write_profile(profile)
    return {"ok": True}


@app.post("/api/profile/analyze-resume")
def analyze_resume():
    """Analyze resume against recent job patterns, return health score and suggestions."""
    profile = read_profile()
    settings = get_settings()
    if not JOBS_DB.exists():
        return {"score": 0, "suggestions": ["No jobs scraped yet \u2014 run the scraper first"], "dimensions": {}}

    con = sqlite3.connect(JOBS_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT title, description FROM jobs "
        "WHERE description IS NOT NULL AND description != \'\' "
        "ORDER BY discovered_at DESC LIMIT 30"
    ).fetchall()
    con.close()

    if not rows:
        return {"score": 0, "suggestions": ["No job descriptions found"], "dimensions": {}}

    jd_sample = "\n---\n".join(
        f"{r['title']}: {(r['description'] or '')[:300]}" for r in rows[:10]
    )
    wiki = profile.get("wiki", "") or profile.get("resume", "")[:1000]

    prompt = (
        "Analyze this candidate's profile against recent job postings and provide:\n"
        "1. A resume health score (0-100)\n"
        "2. 3-5 specific, actionable suggestions to improve their match rate\n"
        "3. Scores (0-100) for 4 dimensions: keyword_coverage, specificity, recency_signal, seniority_alignment\n\n"
        f"Recent job postings sample:\n{jd_sample}\n\n"
        f"Candidate profile:\n{wiki[:800]}\n\n"
        'Return JSON:\n{"score": 72, "suggestions": ["Add MEDDIC to skills (appears in 8/10 recent jobs)", "..."], '
        '"dimensions": {"keyword_coverage": 68, "specificity": 65, "recency_signal": 80, "seniority_alignment": 75}}'
    )

    resp = _openrouter_call(settings, prompt, timeout=60)
    try:
        data = json.loads(resp)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        profile["resume_health"] = data
        write_profile(profile)
        return data
    except Exception:
        return {"score": 0, "suggestions": ["Analysis failed \u2014 try again"], "dimensions": {}}


@app.post("/api/run-scraper")
def run_scraper():
    try:
        resp = requests.post(f"{SCRAPER_URL}/run", timeout=SCRAPER_REQUEST_TIMEOUT)
        resp.raise_for_status()
        return {"ok": True, "message": "Scraper started — jobs will appear as they're scored."}
    except Exception as e:
        raise HTTPException(502, f"Could not reach scraper: {e}")


@app.post("/api/deep-search")
def deep_search():
    try:
        resp = requests.post(f"{SCRAPER_URL}/run?deep=1", timeout=SCRAPER_REQUEST_TIMEOUT)
        resp.raise_for_status()
        return {"ok": True, "message": "Deep search started — finding all currently listed jobs. This may take a few minutes."}
    except Exception as e:
        raise HTTPException(502, f"Could not reach scraper: {e}")


@app.get("/api/scraper-status")
def scraper_status():
    last_run = None
    if JOBS_DB.exists():
        try:
            con = sqlite3.connect(JOBS_DB)
            row = con.execute(
                "SELECT discovered_at FROM jobs ORDER BY discovered_at DESC LIMIT 1"
            ).fetchone()
            con.close()
            if row:
                last_run = row[0]
        except Exception:
            pass
    return {"last_run": last_run}


@app.post("/api/test-send", response_model=TestSendResponse)
def test_send() -> TestSendResponse:
    settings = get_settings()
    profile = read_profile()
    notif = profile["notification"]

    jobs = query_jobs(notif["score_threshold"], notif["max_jobs_per_email"],
                      notif.get("max_job_age_days", 7))
    if not jobs:
        return TestSendResponse(ok=False, message="No jobs above threshold — nothing to send.")

    date_str = datetime.now(ZoneInfo(notif["timezone"])).strftime("%B %d, %Y")
    top_score = jobs[0]["score"]

    subject = "[TEST] " + notif["email_subject"].format(count=len(jobs), top_score=top_score)
    html = build_email_html(jobs, date_str, settings.site_url)

    try:
        send_email(settings, subject, html)
    except Exception as e:
        raise HTTPException(500, f"Email failed: {e}")

    tg_text = "[TEST] " + notif["telegram_message"].format(count=len(jobs), top_score=top_score)
    send_message(settings, tg_text)

    return TestSendResponse(ok=True, message=f"Test sent — {len(jobs)} jobs, top score {top_score}")
