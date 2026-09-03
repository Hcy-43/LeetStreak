FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /srv

# Dependencies first so code edits do not bust the layer cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY scripts ./scripts

ENV PATH="/srv/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# State lives in Postgres now, so the container itself is disposable and needs no
# volume. DATABASE_URL comes from the environment.
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
