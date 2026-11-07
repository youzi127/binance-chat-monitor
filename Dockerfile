FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY pyproject.toml ./
RUN mkdir -p src/binance_chat_api && \
    touch src/binance_chat_api/__init__.py && \
    touch README.md && \
    pip install '.[browser]' && \
    python -m playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --create-home app && \
    mkdir -p /app/data /home/app/.cache && \
    chown -R app:app /app/data /home/app && \
    chmod -R a+rX /ms-playwright

COPY README.md ./
COPY src ./src
RUN pip install --no-deps --force-reinstall .

USER app
EXPOSE 8102

CMD ["binance-chat", "serve"]
