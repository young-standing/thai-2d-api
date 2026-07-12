FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/var/lib/thai-2d/thai_2d.sqlite3 \
    HEADLESS=true

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --requirement requirements.txt

COPY --chown=pwuser:pwuser . .
RUN mkdir -p /var/lib/thai-2d && chown pwuser:pwuser /var/lib/thai-2d

USER pwuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)); assert d['database']=='ok'"]

ENTRYPOINT ["python", "deploy/container_entrypoint.py"]
CMD ["api"]
