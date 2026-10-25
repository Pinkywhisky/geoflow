FROM python:3.12-slim
LABEL org.opencontainers.image.licenses="AGPL-3.0-only"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GEOFLOW_DATA_DIR=/data XDG_CACHE_HOME=/tmp/.cache XDG_CONFIG_HOME=/tmp/.config
WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        libfontconfig1 \
        libgl1 \
        libx11-6 \
        libx11-xcb1 \
        libxcb-icccm4 \
        libxcb-image0 \
        libxcb-keysyms1 \
        libxcb-randr0 \
        libxcb-render-util0 \
        libxcb-shape0 \
        libxcb-xkb1 \
        libxkbcommon-x11-0 \
        xauth \
        xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && groupadd --system geoflow \
    && useradd --system --gid geoflow --home-dir /tmp geoflow \
    && mkdir -p /data \
    && chown geoflow:geoflow /app /data
COPY --chown=geoflow:geoflow app ./app
COPY --chown=geoflow:geoflow LICENSE ./LICENSE
USER geoflow
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
