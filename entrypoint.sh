#!/bin/sh
set -e

# 如果你想第一次進來就先跑一次：docker run ... --once
if [ "${1:-}" = "--once" ]; then
  exec python /app/sync_moj_law.py --repo_id lianghsun/tw-law --workdir /data --push
fi

# cron 需要這個目錄
mkdir -p /data

# 啟動 cron（前景）
exec cron -f