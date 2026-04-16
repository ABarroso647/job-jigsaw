FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && apt-get install -y tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY profile-editor/pyproject.toml profile-editor/uv.lock ./
RUN uv sync --frozen --no-dev

COPY lib/ lib/
COPY profile-editor/main.py .
COPY profile-editor/templates/ templates/
COPY profile-editor/static/ static/

ENV PYTHONPATH=/app/lib

EXPOSE 3006

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3006"]
