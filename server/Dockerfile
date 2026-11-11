FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl rclone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir uv \
    && uv venv /srv/.venv \
    && uv pip install --python /srv/.venv/bin/python -r pyproject.toml

ENV MODELDOCK_DATA_DIR=/data \
    PATH="/srv/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8820

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8820"]
