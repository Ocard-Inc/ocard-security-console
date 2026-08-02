# Ocard Security Log Console
#
# 這個映像同時是 API、SPA 靜態檔來源，以及五分鐘檢查排程（排程跑在 FastAPI
# lifespan 內，見 api/app.py）。因此**只能跑單一 worker、單一實例** ——
# 兩個 process 會各自跑一份 scheduler_loop，同一個 tick 被評估兩次，
# events 的 cooldown 狀態機會發出重複通知。理由詳見 docs/deploy-gcp.md。
#
# 狀態（state/monitor.db）不在映像裡，由 persistent disk 掛進 /app/state。
# .env 也不在映像裡，由 entrypoint 在啟動時從 Secret Manager 取得。

# ── 依賴解析：把 uv.lock 轉成帶 hash 的 requirements.txt ────────────────
# 刻意不在最終映像留 uv：實際安裝的版本由 uv.lock 鎖定，與這一層用哪個 uv
# 版本無關，所以這裡不必也不該去釘 uv 的映像標籤。
FROM python:3.12-slim-bookworm AS deps

RUN pip install --no-cache-dir uv

WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-editable -o /requirements.txt


# ── 執行映像 ────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

# tzdata：core/timewin.py 以 zoneinfo 取 Asia/Taipei。slim 映像不保證帶
# /usr/share/zoneinfo，缺了會在啟動時拋 ZoneInfoNotFoundError。
# ca-certificates：Slack webhook、Anthropic API、ROS 驗證都走 HTTPS。
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Taipei

# 依賴獨立成一層：只有 pyproject.toml / uv.lock 變動時才重跑
COPY --from=deps /requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /app

# data/cloud_ranges/ 必須進映像：intel/ranges.py 的離線 CIDR 比對讀它，
# 少了這些檔案分類會全部變成 unknown（正確但無用的降級）。
COPY src/    ./src/
COPY config/ ./config/
COPY web/    ./web/
COPY data/   ./data/
COPY docker/entrypoint.py ./docker/

# 掛載點。真正的內容由 persistent disk 提供；entrypoint 會斷言它掛上了。
RUN mkdir -p /app/state/logs /app/outputs

EXPOSE 8600

# 不用 HEALTHCHECK：konlet（COS 的容器啟動器）不讀 Dockerfile 的 HEALTHCHECK，
# 存活檢查由 ROS 那端打 /healthz。
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
