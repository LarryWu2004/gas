FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GAS_BUNDLE_DIR=/app \
    GAS_DATA_DIR=/data \
    GAS_DIAGNOSIS_HOST=0.0.0.0 \
    GAS_DIAGNOSIS_PORT=8080 \
    GAS_CHROMIUM_PATH=/usr/bin/chromium

WORKDIR /app

RUN groupadd --system diagnosis \
    && useradd --system --gid diagnosis --home-dir /app diagnosis

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-noto-cjk fonts-liberation \
    && test -x /usr/bin/chromium \
    && chromium --version \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY gas_diagnosis ./gas_diagnosis
COPY models ./models

RUN mkdir -p /data/outputs/web_uploads /data/outputs/web_diagnosis \
    && chown -R diagnosis:diagnosis /data

USER diagnosis

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request; response=urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3); sys.exit(0 if response.status == 200 else 1)"

CMD ["python", "-m", "gas_diagnosis.production"]
