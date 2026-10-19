# Multi-arch image (linux/amd64 and linux/arm64 – the python base image ships both).
#   docker build -t accounting-engine .
#   docker compose up -d --build                      (plain, port 8501)
#   docker compose -f docker-compose.yml -f docker-compose.domain.yml up -d --build   (domain + HTTPS via Caddy)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_SHOW_EMAIL_PROMPT=false AE_DATA_DIR=/app/data

# flac: only needed if you upload .flac voice recordings
RUN apt-get update && apt-get install -y --no-install-recommends flac && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY engine ./engine
COPY ui ./ui
COPY fixtures ./fixtures
COPY .streamlit/config.toml ./.streamlit/config.toml
# run as an unprivileged user; /app/data is a bind mount owned by this uid (see docker-compose.yml)
RUN useradd --system --uid 10001 --create-home app && mkdir -p /app/data && chown -R app:app /app
USER app

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).status==200 else 1)"
CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
