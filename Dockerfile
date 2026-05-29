# Playwright Python image already includes Chromium + all system libs.
# Pinning to a recent v1.48 jammy build to match requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BROWSER_HEADLESS=true \
    HUMAN_LOOP_BACKEND=polling \
    STATE_BACKEND=sqlite \
    PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# SQLite + uploads live on the container filesystem for MVP.
# Wiped on redeploy. Move to RDS + S3 once UI is stable.
RUN mkdir -p /app/data /app/uploads

EXPOSE 8000

# App Runner sends traffic to PORT. Single worker — the agent holds Playwright
# browser state in-process, multi-worker would split that across workers.
CMD ["sh", "-c", "uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
