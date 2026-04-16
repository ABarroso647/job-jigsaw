FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && apt-get install -y cron tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scraper/pyproject.toml scraper/uv.lock ./
RUN uv sync --frozen --no-dev

COPY lib/ lib/
COPY scraper/scrape.py scraper/trigger_server.py ./

ENV PYTHONPATH=/app/lib

# Scrape every 6 hours
RUN echo "0 */6 * * * . /etc/environment; cd /app && .venv/bin/python scrape.py >> /proc/1/fd/1 2>&1" \
      > /etc/cron.d/scraper \
    && chmod 0644 /etc/cron.d/scraper \
    && crontab /etc/cron.d/scraper

# Dump env for cron, start trigger server in background, then start cron
CMD ["sh", "-c", "printenv > /etc/environment && .venv/bin/python trigger_server.py & cron -f"]
