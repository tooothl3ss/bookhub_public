FROM python:3.12-slim AS runtime
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bookhub ./bookhub
COPY myshelf/static ./myshelf/static
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY licenses ./licenses
ENV BOOKHUB_DATA=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8088 8089
CMD ["python", "-m", "uvicorn", "bookhub.application:create_web_app", "--factory", "--host", "0.0.0.0", "--port", "8088"]

FROM runtime AS tests
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt && apt-get update -qq && apt-get install -y --no-install-recommends luajit curl nodejs && rm -rf /var/lib/apt/lists/*
ENV PYTHONPATH=/src
WORKDIR /src
CMD ["bash", "scripts/test.sh"]

FROM runtime AS release
