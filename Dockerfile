# The API image: pipeline (CrewAI + MCP catalogue server + pricing) behind FastAPI.
# Built by docker-compose.yml (service "api"); the review screen has its own image (ui/Dockerfile).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    MODEL_CACHE=/opt/models

WORKDIR /app

# Packages first, code second: a code edit rebuilds in seconds.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project

COPY app ./app
COPY fixtures ./fixtures
# Sample emails for the demo inbox ("Add to inbox" in the review screen). Synthetic, see eval/cases.py.
COPY eval/testset/emails ./eval/testset/emails

# Download the embedding model and embed the catalogue now, so the container starts offline and fast.
RUN python -c "from app.catalogue.index import get_catalogue; get_catalogue().warm()"

VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
