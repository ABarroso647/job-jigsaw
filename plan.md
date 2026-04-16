# Job Scout — Project Plan
> Handoff document for Claude Code. Read this fully before writing any code.

---

## What we're building

A self-hosted job scouting system running on TrueNAS via Dockge (Docker). It scrapes job boards daily, AI-scores every job against a candidate profile, and sends a formatted email digest + Telegram ping at 6pm every day. The candidate never needs to touch the NAS or any dashboard — she just gets an email and a Telegram message.

Additionally a simple local web UI to edit the candidate profile, resume, and keywords without touching any files directly.

---

## The spirit of this project

The original inspiration was **job-scout** (github.com/weberwcwei/job-scout) — simple, config-driven, email/Telegram notifications, scores jobs 0-100. We are using **JobOps** (github.com/DaKheera47/job-ops) as the engine instead because it has smarter LLM-based scoring (reads full job descriptions, not just keyword matching) and better job sources, but the experience should FEEL like job-scout — a daily email, a Telegram ping, a simple page to tweak things. The candidate never sees the JobOps dashboard unless we explicitly set up Tailscale later.

---

## Candidate profile

- **Name:** [GF's name — fill in]
- **Location:** Toronto / GTA area (Greater Toronto Area, Ontario, Canada)
- **Current role:** Sales Associate at EQ3 (furniture retailer), becoming Assistant Store Manager in ~1 week
- **Degree:** Finance
- **Background:** Several years in furniture/home goods sales, deep industry knowledge, consultative selling experience

### Job targets (primary)
- BDR (Business Development Representative)
- SDR (Sales Development Representative)
- Account Executive (SMB/mid-market)
- Inside Sales Representative
- Business Development Manager
- Territory Sales Representative (furniture, home goods, interior industry)
- Account Manager
- Sales Representative at furniture/home goods manufacturers or distributors
- Trade Sales Rep (selling to architects, interior designers, contractors)

### Job targets (secondary)
- Retail Assistant Manager / Store Manager (lifestyle/home brands)
- Brand Sales Representative
- Finance-adjacent: wealth management associate, banking sales, insurance sales (Sun Life, Manulife etc — finance degree relevant here)

### Key companies / industries to boost
- Toronto SaaS/tech companies (Shopify, Clio, Ritual, Wealthsimple, etc.)
- Furniture/home goods manufacturers and distributors
- Interior design industry vendors
- Fintech SaaS (finance degree relevant)
- Consumer lifestyle brands

### Location logic
- Primary: Toronto / GTA in-person or hybrid roles
- Secondary: Canada-wide remote roles
- Penalize heavily: US-only remote, requires US work authorization, US citizens only

---

## Architecture

```
TrueNAS (Dockge)
│
├── jobops            → scrapes job boards, AI-scores, stores in SQLite
│                       dashboard at NAS-IP:3005 (internal only for now)
│
├── notifier          → Python script, runs at 6pm daily via cron
│                       reads jobs.db, sends HTML email digest + Telegram ping
│
├── profile-editor    → FastAPI + plain HTML web app at NAS-IP:3006
│                       UI to edit profile.yaml, upload resume, get AI keyword suggestions
│                       preview tonight's digest without sending
│
└── shared volumes
    ├── /data/jobs.db           → SQLite database (JobOps writes, notifier reads)
    ├── /data/profile.yaml      → ⭐ single source of truth, all containers read this
    ├── /data/sent_jobs.db      → notifier tracks which jobs already sent (no duplicates)
    └── /data/secrets.env       → credentials, never committed to git
```

---

## Notification flow

```
Every 6 hours (JobOps internal schedule):
  → Scrapes Indeed, LinkedIn, Adzuna, Hiring Cafe
  → AI scores every job 0-100 against profile.yaml
  → Stores in jobs.db

6:00pm daily (notifier cron):
  → Reads jobs.db for jobs scored 55+ in last 24hrs not already sent
  → Builds HTML email digest (max 15 jobs, sorted by score desc)
  → Sends email via Gmail SMTP
  → Sends Telegram ping: "📋 14 new jobs in your email · top match 82/100 ✉️"
  → Marks jobs as sent in sent_jobs.db
```

---

## The profile.yaml — single source of truth

This is the ONE file that controls everything. Editing this file affects:
- What jobs get scraped (search terms, location)
- How jobs get scored (keywords, weights)
- What gets notified (threshold, max jobs)
- What the AI scorer knows about the candidate (resume text)

**No container restarts needed when this file changes.** Every container reads it fresh on each run.

```yaml
# ─────────────────────────────────────────
# CANDIDATE RESUME — paste full resume text here
# ─────────────────────────────────────────
resume: |
  [paste resume here]

# ─────────────────────────────────────────
# WHAT SHE IS LOOKING FOR — plain English description
# used by AI scorer for context
# ─────────────────────────────────────────
description: |
  Looking for BDR, SDR, or Account Executive roles at Toronto-area tech/SaaS companies,
  as well as territory or trade sales roles in the furniture and home goods industry.
  Open to remote roles within Canada. Finance degree background makes fintech SaaS
  and financial services sales roles relevant too.

# ─────────────────────────────────────────
# JOB SEARCH TERMS — what gets searched on job boards
# ─────────────────────────────────────────
search:
  terms:
    - BDR
    - SDR
    - Business Development Representative
    - Account Executive
    - Inside Sales
    - Territory Sales Representative
    - Account Manager sales
    - furniture sales representative
    - home goods sales
    - trade sales representative
    - retail assistant manager
    - sales development

  locations:
    - Toronto, ON
    - Greater Toronto Area
    - Canada  # catches remote Canadian postings

  remote: true
  hours_old: 24       # only jobs posted in last 24 hours
  results_per_site: 25

# ─────────────────────────────────────────
# SCORING — tweak these anytime, no restart needed
# ─────────────────────────────────────────
scoring:
  boost:
    - { keyword: "BDR",                  weight: 20 }
    - { keyword: "SDR",                  weight: 20 }
    - { keyword: "SaaS",                 weight: 15 }
    - { keyword: "furniture",            weight: 20 }
    - { keyword: "home goods",           weight: 20 }
    - { keyword: "interior design",      weight: 15 }
    - { keyword: "trade sales",          weight: 18 }
    - { keyword: "CRM",                  weight: 10 }
    - { keyword: "Salesforce",           weight: 10 }
    - { keyword: "HubSpot",              weight: 10 }
    - { keyword: "quota",                weight: 10 }
    - { keyword: "pipeline",             weight: 10 }
    - { keyword: "fintech",              weight: 12 }
    - { keyword: "proptech",             weight: 10 }
    - { keyword: "Toronto",              weight: 15 }
    - { keyword: "GTA",                  weight: 15 }
    - { keyword: "remote Canada",        weight: 10 }
    - { keyword: "finance",              weight: 8  }
    - { keyword: "account management",   weight: 12 }
    - { keyword: "consultative",         weight: 10 }

  penalize:
    - { keyword: "US citizens only",                          weight: -50 }
    - { keyword: "must be authorized to work in the US",      weight: -50 }
    - { keyword: "US work authorization",                     weight: -50 }
    - { keyword: "no sponsorship",                            weight: -30 }
    - { keyword: "warehouse",                                 weight: -20 }
    - { keyword: "manufacturing operator",                    weight: -20 }
    - { keyword: "10+ years",                                 weight: -25 }
    - { keyword: "15+ years",                                 weight: -30 }

# ─────────────────────────────────────────
# NOTIFICATION SETTINGS
# ─────────────────────────────────────────
notification:
  score_threshold: 55       # only jobs at or above this score get sent
  max_jobs_per_email: 15    # cap so email isn't overwhelming
  schedule: "0 18 * * *"   # 6pm daily Toronto time
  timezone: "America/Toronto"
  email_subject: "📋 {count} new jobs today · top match {top_score}/100"
  
  # Telegram ping message (sent after email)
  telegram_message: "📋 {count} new jobs in your email · top match {top_score}/100 ✉️"
```

---

## File structure to create

```
jobscout/                         ← project root, open this in Claude Code
├── PLAN.md                       ← this file
├── docker-compose.yml            ← all 3 containers
├── secrets.env.template          ← template, user fills in and renames to secrets.env
├── data/
│   └── profile.yaml              ← pre-filled, user adds resume text
├── notifier/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── notify.py                 ← the main notifier script
└── profile-editor/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py                   ← FastAPI app
    └── templates/
        └── index.html            ← the UI (tabs: Resume, Keywords, Settings, Preview)
```

---

## Files to write (in this order)

### 1. `docker-compose.yml`
Three services:
- `jobops` — pulls `ghcr.io/dakheera47/job-ops:latest`, port 3005, mounts ./data
- `notifier` — builds from ./notifier, mounts ./data (reads jobs.db + profile.yaml, writes sent_jobs.db)
- `profile-editor` — builds from ./profile-editor, port 3006, mounts ./data (reads+writes profile.yaml)

Shared volume: `./data` mounted into all three containers.
Env file: `secrets.env` for all credential env vars.

JobOps env vars needed:
- `OPENROUTER_API_KEY`
- `JOBSPY_LOCATION=Toronto, ON`
- `JOBSPY_COUNTRY_INDEED=Canada`
- `JOBSPY_SITES=indeed,linkedin`
- `JOBSPY_HOURS_OLD=24`
- `JOBSPY_RESULTS_WANTED=25`
- `TZ=America/Toronto`

### 2. `secrets.env.template`
```
OPENROUTER_API_KEY=your_key_here
OPENROUTER_MODEL=deepseek/deepseek-r1:free
ADZUNA_APP_ID=your_id_here
ADZUNA_APP_KEY=your_key_here
GMAIL_FROM=her_email@gmail.com
GMAIL_TO=her_email@gmail.com
GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxxxxxx
TELEGRAM_BOT_TOKEN=your_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

### 3. `notifier/notify.py`
- Reads `profile.yaml` for threshold, max_jobs, timezone, message templates
- Reads `data/jobs.db` — query jobs where score >= threshold AND date_posted >= 24hrs ago
- Reads `data/sent_jobs.db` — skip any job_url already sent
- Sorts by score descending, takes top max_jobs
- Builds HTML email (see email format below)
- Sends via Gmail SMTP (smtplib, port 587, TLS)
- Sends Telegram message via Bot API
- Writes sent job URLs to sent_jobs.db with timestamp
- Logs everything to stdout so Dockge shows it in container logs

### 4. `notifier/Dockerfile`
Simple python:3.12-slim, installs requirements, runs notify.py via cron at schedule from profile.yaml (default 6pm America/Toronto).

### 5. `profile-editor/main.py`
FastAPI app with these routes:
- `GET /` — serve index.html
- `GET /api/profile` — return current profile.yaml as JSON
- `POST /api/profile` — save updated profile.yaml
- `POST /api/analyse` — takes resume text + description, calls OpenRouter API, returns suggested job titles + keywords + weights as JSON
- `GET /api/preview` — reads jobs.db, returns jobs that would be in tonight's email given current profile.yaml settings (does NOT send)
- `POST /api/test-send` — sends a real test email + Telegram ping right now

### 6. `profile-editor/templates/index.html`
Clean, minimal HTML + vanilla JS (no frameworks, keep it simple). Four tabs:
- **Resume** — textarea for resume, textarea for plain-English description, "Analyse with AI" button, results panel showing suggested titles/keywords with checkboxes, "Save Selected" button
- **Keywords** — editable table of boost keywords + weights, editable table of penalize keywords + weights, "Add row" button on each, score threshold slider with live readout ("X jobs in DB would be sent at this threshold")
- **Settings** — search terms list (add/remove), location settings, remote toggle, max jobs per email, email send time
- **Preview** — shows tonight's digest as it would appear in email, "Send test email now" button

---

## Email format

HTML email, clean and readable on mobile. Example structure:

```
Subject: 📋 8 new jobs today · top match 82/100

─────────────────────────────
🏆 82/100 · Account Executive · Clio
📍 Toronto, ON · Hybrid · Full-time
Posted: today
[View Job →]
─────────────────────────────
74/100 · BDR · Shopify
📍 Remote (Canada) · Full-time
Posted: today
[View Job →]
─────────────────────────────
...
─────────────────────────────
Sent by your job scout · Edit profile → NAS-IP:3006
```

Each job card should show: score (with colour — green 70+, yellow 55-69), title, company, location, remote/hybrid/in-person badge, job type, posted date, direct apply link button.

---

## Key decisions / constraints

- **Location:** Toronto / GTA only for in-person. Canada-wide for remote. Penalize US-only.
- **Timezone:** America/Toronto throughout
- **Score threshold:** Start at 55, easy to adjust in profile.yaml or via UI
- **Notification time:** 6pm daily
- **Max jobs per email:** 15
- **AI model:** deepseek/deepseek-r1:free via OpenRouter (free tier, no cost)
- **No login/auth on profile editor** — it's local only, Tailscale gates it later
- **No resume tailoring / PDF generation yet** — JobOps has this built in, we enable it later when Tailscale is set up
- **NAS OS:** TrueNAS
- **Container manager:** Dockge
- **All containers in one compose file** — single deploy in Dockge
- **profile.yaml is the ONLY file that needs editing** after initial setup
- **No container restarts needed** when profile.yaml changes — all containers read it fresh on each run

---

## What NOT to build yet (future phases)

- Tailscale remote access
- RxResume integration (resume tailoring + PDF generation) — JobOps supports this natively, just needs enabling
- Login/auth on profile editor
- Mobile app
- Multiple candidate profiles
- Application tracking (JobOps has this via Gmail OAuth — enable later)

---

## Credentials needed before deploy (user must gather these)

| Credential | Where to get it | Notes |
|---|---|---|
| OpenRouter API key | openrouter.ai → sign up → API Keys | Free, no card needed |
| Adzuna App ID + Key | developer.adzuna.com | Free, good Canadian coverage |
| Gmail app password | myaccount.google.com/apppasswords | Needs 2FA enabled on Gmail |
| Telegram bot token | Message @BotFather → /newbot | Free |
| Telegram chat ID | Message @userinfobot | Copy the ID it returns |

---

## Start here

Write files in this order:
1. `docker-compose.yml`
2. `secrets.env.template`
3. `data/profile.yaml`
4. `notifier/requirements.txt`
5. `notifier/Dockerfile`
6. `notifier/notify.py`
7. `profile-editor/requirements.txt`
8. `profile-editor/Dockerfile`
9. `profile-editor/main.py`
10. `profile-editor/templates/index.html`

After all files are written, do a full review pass to make sure:
- All containers share the same ./data volume correctly
- profile.yaml path is consistent across all three containers
- Timezone is America/Toronto everywhere
- secrets.env variables are referenced consistently
- No hardcoded credentials anywhere
