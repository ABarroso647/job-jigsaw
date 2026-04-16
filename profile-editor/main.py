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
    "search": {
        "terms": [],
        "locations": ["Toronto, ON"],
        "hours_old": 24,
        "results_per_site": 25,
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
    },
}


# ── Startup initialization ────────────────────────────────────────────────────

def _init_profile() -> None:
    if not PROFILE_PATH.exists():
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        write_profile(DEFAULT_PROFILE)
        log.info("Created default profile.yaml at %s", PROFILE_PATH)


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


def query_jobs(threshold: int, max_jobs: int) -> list[dict]:
    if not JOBS_DB.exists():
        return []
    con = None
    try:
        con = sqlite3.connect(JOBS_DB)
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT title, employer, location, job_url, suitability_score,
                   date_posted, is_remote, job_type, user_rating
            FROM jobs
            WHERE suitability_score >= ?
              AND (user_rating IS NULL OR user_rating != -1)
              AND (hidden = 0 OR hidden IS NULL)
            ORDER BY suitability_score DESC
            LIMIT ?
        """, (threshold, max_jobs)).fetchall()
        results = []
        for r in rows:
            job = dict(r)
            job["company"] = job.pop("employer", "")
            job["score"] = job.pop("suitability_score", 0)
            results.append(job)
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
        jobs = query_jobs(notif["score_threshold"], notif["max_jobs_per_email"])
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
        "liked":    "AND user_rating = 1",
        "disliked": "AND user_rating = -1",
        "applied":  "AND is_applied = 1",
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
                   discovered_at, user_rating, notes, is_applied
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
    elif filter == "applied":
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

    jobs = query_jobs(notif["score_threshold"], notif["max_jobs_per_email"])
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
