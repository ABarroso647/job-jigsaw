 # 🧩 Job Jigsaw

  A self-hosted job hunting system that scrapes job boards, scores listings against your profile using an LLM, and sends you a daily email digest. Reply to emails to approve/reject AI-suggested profile updates that improve your search over time.

  ## Features

  - **Scraper** — scrapes Indeed & LinkedIn every 6 hours using your search terms and locations
  - **AI Scoring** — every job is scored 0–100 against your resume and preferences via OpenRouter
  - **Daily Digest** — email at 6pm with your top matches, sorted by score
  - **Profile Editor** — web UI to manage your resume, keywords, boost/penalize weights, and search settings
  - **AI Insights** — analyzes your liked/disliked jobs and proposes profile updates (new boost keywords, penalize keywords, search terms)
  - **Email Reply Flow** — approve, reject, or refine proposals by replying to the email. No app needed.
  - **Insights History** — view and revert past AI-applied profile changes

  ## Stack

  - Python, FastAPI, SQLite
  - `python-jobspy` for scraping
  - OpenRouter for LLM scoring and insights
  - Gmail (SMTP + IMAP) for email digest and reply flow
  - Docker + Docker Compose
  - Designed for self-hosting on a NAS via Dockge

  ## Setup

  ### 1. Clone

  ```bash
  git clone git@github.com:ABarroso647/job-jigsaw.git
  cd job-jigsaw

  2. Configure

  Copy .env.example to .env and fill in your values:

  cp .env.example .env

  ┌────────────────────┬───────────────────────────────────────────────────────┐
  │      Variable      │                      Description                      │
  ├────────────────────┼───────────────────────────────────────────────────────┤
  │ GMAIL_FROM         │ Gmail address to send from                            │
  ├────────────────────┼───────────────────────────────────────────────────────┤
  │ GMAIL_TO           │ Email address to receive digests                      │
  ├────────────────────┼───────────────────────────────────────────────────────┤
  │ GMAIL_APP_PASSWORD │ Gmail app password                                    │
  ├────────────────────┼───────────────────────────────────────────────────────┤
  │ OPENROUTER_API_KEY │ OpenRouter API key                                    │
  ├────────────────────┼───────────────────────────────────────────────────────┤
  │ OPENROUTER_MODEL   │ Model to use (default: meta-llama/llama-3.3-70b:free) │
  ├────────────────────┼───────────────────────────────────────────────────────┤
  │ SITE_URL           │ Your NAS URL e.g. http://192.168.1.100:3006           │
  ├────────────────────┼───────────────────────────────────────────────────────┤
  │ TELEGRAM_BOT_TOKEN │ (Optional) Telegram bot token                         │
  ├────────────────────┼───────────────────────────────────────────────────────┤
  │ TELEGRAM_CHAT_ID   │ (Optional) Telegram chat ID                           │
  └────────────────────┴───────────────────────────────────────────────────────┘

  3. Run

  docker compose up -d --build

  Open the profile editor at http://localhost:3006 and fill in your resume and search settings.

  Usage

  - Profile Editor at :3006 — set your resume, description, keywords, score threshold, and search terms
  - Preview tab — see what jobs would be in tonight's email, like/dislike to train the AI
  - History tab — browse all sent jobs filtered by date, rate and add notes
  - Insights tab — view and approve/reject pending AI profile update proposals, see history of past changes

  Email Reply Flow

  When you click AI Insights, a proposal is generated and emailed to you. Reply with:
  - APPROVE — applies the changes to your profile
  - REJECT — discards the proposal
  - Anything else — the AI revises the proposal based on your feedback and replies back

  Updating

  git pull
  docker compose up -d --build
