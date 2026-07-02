FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install --requirement requirements.txt

FROM python:3.12-slim-bookworm

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_CONFIG_HOME=/tmp/.config

COPY --from=builder /opt/venv /opt/venv

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium curl fontconfig fonts-dejavu-core fonts-noto-core fonts-noto-extra fonts-noto-ui-core fonts-noto-color-emoji libraqm0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 farstar \
    && useradd --system --uid 10001 --gid farstar --home-dir /app --shell /usr/sbin/nologin farstar

WORKDIR /app
COPY --chown=farstar:farstar bot ./bot

USER 10001:10001

CMD ["python", "-m", "bot.main"]
