FROM python:3.12-slim

WORKDIR /app

# WeasyPrint renders through pango/cairo and needs a font that covers the CV's characters.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 \
        libgdk-pixbuf-2.0-0 libffi8 shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
# Diagnostics run with `docker compose exec`, where the endpoint and the database are both
# reachable. They are never started by the daemon — see scripts/README.md.
COPY scripts/ ./scripts/

ENV PYTHONPATH=/app/src

CMD ["python", "src/main.py"]
