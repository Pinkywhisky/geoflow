FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GEOFLOW_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system geoflow \
    && useradd --system --gid geoflow --home-dir /app geoflow \
    && mkdir -p /data \
    && chown geoflow:geoflow /app /data
COPY --chown=geoflow:geoflow app ./app
COPY --chown=geoflow:geoflow samples ./samples
COPY --chown=geoflow:geoflow tests ./tests
COPY --chown=geoflow:geoflow pytest.ini .
USER geoflow
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
