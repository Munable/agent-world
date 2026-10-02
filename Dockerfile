FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# Runtime dependencies are pinned in the package's existing requirements file.
COPY requirements.txt ./
RUN python -m pip install -r requirements.txt
COPY pyproject.toml README.md LICENSE ./
COPY agent_world ./agent_world
COPY examples ./examples
RUN python -m pip install --no-deps .
COPY deploy/railway.py ./deploy/railway.py

# Railway mounts volumes as root. The entrypoint prepares only its dedicated
# data directory, then permanently drops to uid/gid 10001 before importing the app.
# Attach a persistent Railway volume at /data; do not use an anonymous VOLUME.
CMD ["python", "/app/deploy/railway.py"]
