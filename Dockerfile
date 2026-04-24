# Use official Python runtime as base image
FROM python:3.11-slim

# Set working directory in container
WORKDIR /app

# Set environment variables
# Prevents Python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-dejavu-core \
      && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY bot/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot/main.py bot/core.py bot/fabry_score_weights.json ./

# Copy logs directory
RUN mkdir -p logs

# Run the bot
CMD ["python", "main.py"]
