# Use the official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Set work directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY api.py send_jobbyo.py approve_jobs.py company_ingestion.py ./

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
