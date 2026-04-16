FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && apt-get install -y cron tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY notifier/pyproject.toml notifier/uv.lock ./
RUN uv sync --frozen --no-dev

COPY lib/ lib/
COPY notifier/notify.py notifier/poll_replies.py ./

ENV PYTHONPATH=/app/lib

# Cron jobs:
#   notify.py     — 6pm daily
#   poll_replies  — every 15 min (exits immediately if no active proposal)
RUN printf '%s\n' \
      '0 18 * * * . /etc/environment; cd /app && .venv/bin/python notify.py >> /proc/1/fd/1 2>&1' \
      '*/15 * * * * . /etc/environment; cd /app && .venv/bin/python poll_replies.py >> /proc/1/fd/1 2>&1' \
    > /etc/cron.d/notifier \
    && chmod 0644 /etc/cron.d/notifier \
    && crontab /etc/cron.d/notifier

# Dump all env vars (including secrets from env_file) so cron can read them,
# then start cron in foreground
CMD ["sh", "-c", "printenv > /etc/environment && cron -f"]
