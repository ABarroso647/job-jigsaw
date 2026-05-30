# Job Jigsaw

Self-hosted job scraping and daily email digest system. Scrapes LinkedIn, Indeed, ATS boards, and RSS feeds. Scores jobs against your profile using DeepSeek (evidence-based matching in progress). Sends a ranked daily digest with Jina semantic re-ranking.

## Architecture

```
Scraper (cron daily)
  └── python-jobspy → LinkedIn + Indeed
  └── sources/ats.py → Greenhouse, Lever, Ashby
  └── sources/rss.py → We Work Remotely, Remotive, Jobicy
  └── quality_gate.py → filter noise before LLM
  └── score_job() → DeepSeek scorer
  └── evidence scoring → replaces entity boost (in progress)
  └── jobs.db

Notifier (cron daily)
  └── fetch_unsent_jobs() → staleness filter + score threshold
  └── rerank_with_jina() → Jina Reranker v3
  └── build_email_html() → Gmail-safe digest
  └── reply_parser.py → Gmail IMAP reply hooks (in progress)

Profile Editor (always-on FastAPI)
  └── http://your-ip:3006
  └── Resume, Wiki, Settings, Preview, History, Pipeline tabs
```

## Services

| Service | Path | Port |
|---|---|---|
| Profile Editor | `profile-editor/` | 3006 |
| Scraper | `scraper/` | (cron only) |
| Notifier | `notifier/` | (cron only) |

## Setup

### 1. Copy env file
```bash
cp .env.example .env
# Fill in: OPENROUTER_API_KEY, GMAIL_FROM, GMAIL_TO, GMAIL_APP_PASSWORD, JINA_API_KEY
```

### 2. Run with Docker Compose
```bash
docker compose up -d
```

### 3. Access profile editor
Open http://your-machine-ip:3006

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| OPENROUTER_API_KEY | Yes | LLM scoring (DeepSeek via OpenRouter) |
| OPENROUTER_MODEL | No | Default: `deepseek/deepseek-v4-flash` |
| GMAIL_FROM | Yes | Gmail address that sends the digest |
| GMAIL_TO | Yes | Address that receives the digest |
| GMAIL_APP_PASSWORD | Yes | Gmail App Password (requires 2FA) |
| JINA_API_KEY | No | Jina Reranker v3 — leave blank to skip re-ranking |
| SITE_URL | No | Profile editor URL shown in email footer |
| TELEGRAM_BOT_TOKEN | No | Telegram ping on send |
| TELEGRAM_CHAT_ID | No | Telegram chat ID |

## Running Tests

```bash
# Unit tests (no API calls)
pytest tests/unit/ -v

# LLM quality eval tests (makes real API calls, costs ~$0.01)
pytest tests/eval/ -v

# Frontend tests (requires playwright browsers)
playwright install chromium
pytest tests/frontend/ -v
```

## Profile Configuration (profile.yaml)

Key sections:
- `resume`: free-text resume blob
- `experience[]`: structured work history
- `projects[]`: structured projects
- `skills[]`: skills with evidence levels
- `wiki`: LLM-readable candidate knowledge base
- `search.keywords`: job search terms
- `search.locations`: search locations
- `search.allowed_regions`: location pre-filter (null = disabled)
- `search.require_language`: language pre-filter (null = disabled)
- `search.ats_companies[]`: ATS companies to scrape directly
- `notification.score_threshold`: minimum score to include in digest
- `notification.max_jobs_per_email`: total jobs per digest
- `notification.linkedin_max_jobs`: LinkedIn budget
- `notification.indeed_max_jobs`: Indeed budget
- `scoring.boost[]`: keyword boost list
- `scoring.penalize[]`: keyword penalize list
