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
# Hosts inject the port to bind on. Render defaults to 10000; 8000 keeps a plain
# `docker run -p 8000:8000` working locally.
ENV PORT=8000
EXPOSE 8000

# Shell form so $PORT expands, and exec so uvicorn is PID 1 and gets SIGTERM
# directly - otherwise shutdown hangs until the platform kills it.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
