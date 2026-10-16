# Playwright image already contains Chromium + all system libs (amd64 + arm64).
# The playwright version in requirements.txt MUST match this tag.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bypass_link_fast.py api.py ./

ENV DATA_DIR=/data \
    PORT=8000 \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8000
CMD ["python", "api.py"]
