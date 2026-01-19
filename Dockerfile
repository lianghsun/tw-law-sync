FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Taipei \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    cron \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY sync_moj_law.py /app/sync_moj_law.py
COPY entrypoint.sh /app/entrypoint.sh
COPY crontab /app/crontab

RUN chmod +x /app/entrypoint.sh \
    && crontab /app/crontab

# 讓 cron log 能在 docker logs 看到
CMD ["/app/entrypoint.sh"]