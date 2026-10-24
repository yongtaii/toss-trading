FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.3
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev
RUN useradd --uid 10001 --create-home app && mkdir /app/data && chown app:app /app/data
USER app
EXPOSE 8000
CMD [".venv/bin/uvicorn", "toss_trading.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
