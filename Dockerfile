FROM python:3.12-slim

WORKDIR /app

# WeasyPrint renders through pango/cairo. fonts-liberation supplies Liberation Sans, which is
# metrically compatible with the Arial the master resume is set in; without it the resume
# silently falls back to DejaVu and the line breaks move.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 \
        libgdk-pixbuf-2.0-0 libffi8 shared-mime-info fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
# Diagnostics run with `docker compose exec`, where the endpoint and the database are both
# reachable. They are never started by the daemon — see scripts/README.md.
COPY scripts/ ./scripts/

ENV PYTHONPATH=/app/src

CMD ["python", "src/main.py"]
