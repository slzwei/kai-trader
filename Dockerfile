FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PIP_NO_CACHE_DIR=1

# uv installer is the official Astral one. Pinned via the install URL,
# which gives us a reproducible binary on container build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first for layer caching: a code-only change does
# not invalidate the dep install layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the source and install the project itself.
COPY src/ ./src/
COPY scripts/ ./scripts/
RUN uv sync --frozen --no-dev

CMD ["uv", "run", "python", "-m", "kai_trader.bot.main"]
