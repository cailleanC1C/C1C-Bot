# ---- Base ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System deps kept minimal for Render free tier
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl tini \
    poppler-utils \
 && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m appuser
WORKDIR /app

# Copy only requirements first to leverage Docker layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

# Copy the rest
COPY --chown=appuser:appuser . /app
ENV PYTHONPATH="/app:${PYTHONPATH}"

# Register the internal CoreOps package
RUN pip install -e packages/c1c-coreops \
 && python -c "import c1c_coreops; print('c1c_coreops import OK')"

RUN chown -R appuser:appuser /app

# Expose the health server port (Render sets $PORT; this is just doc)
EXPOSE 10000

# Use tini as init to reap zombies
ENTRYPOINT ["/usr/bin/tini","--"]

# Run as non-root
USER appuser

# Start the bot; ensure your app.py binds health server to $PORT (Render provides it)
CMD ["python","app.py"]
