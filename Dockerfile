# Use the official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Set work directory
WORKDIR /app

# weasyprint (outreach.py's PDF generation) renders real HTML/CSS via Pango
# + Cairo, so it needs these system libs -- pip alone isn't enough.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
        libffi-dev shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY api.py send_jobbyo.py approve_jobs.py company_ingestion.py date_utils.py backfill_boardlinks_from_history.py outreach.py lead_scoring.py outreach_log.py ./

# Create a non-root user and the data dirs the pipeline writes to
RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /app/personas /app/search_contracts /app/run_logs \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Runs the FastAPI wrapper; send_jobbyo.py / approve_jobs.py are invoked by
# it as subprocesses (see api.py), triggered on schedule via the systemd
# timer hitting /run/all, /email/all, /coverage/today.
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
