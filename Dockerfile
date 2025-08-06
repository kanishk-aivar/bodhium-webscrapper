FROM mcr.microsoft.com/playwright/python:v1.53.0-noble

# Set working directory
WORKDIR /app

# Set environment variables early
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8
ENV DEBIAN_FRONTEND=noninteractive

# Update system packages and install essential tools
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    wget \
    gnupg \
    ca-certificates \
    zip \
    unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies with optimizations
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# CRITICAL: Install browsers explicitly with system dependencies
RUN playwright install --with-deps chromium

# Create directories with proper permissions and ownership
RUN mkdir -p /tmp/.crawl4ai /tmp/.crawl4ai_cache /tmp/.crawl4ai_user_data /tmp/screenshots /tmp/crawl_output && \
    chmod 777 /tmp/.crawl4ai /tmp/.crawl4ai_cache /tmp/.crawl4ai_user_data /tmp/screenshots /tmp/crawl_output && \
    chown -R pwuser:pwuser /tmp/.crawl4ai /tmp/.crawl4ai_cache /tmp/.crawl4ai_user_data /tmp/screenshots /tmp/crawl_output

# Use the existing pwuser from Playwright image for security
USER pwuser

# Copy the application files with proper ownership
COPY --chown=pwuser:pwuser app.py .

# Set crawl4ai specific environment variables
ENV CRAWL4AI_DB_PATH=/tmp/.crawl4ai
ENV CRAWL4AI_CACHE_DIR=/tmp/.crawl4ai_cache
ENV CRAWL4AI_BASE_DIRECTORY=/tmp
ENV HOME=/tmp

# Environment variables will be set at runtime by Lambda

# Pre-create Chrome user data directory with proper permissions
RUN mkdir -p /tmp/.crawl4ai_user_data/Default && \
    chmod -R 755 /tmp/.crawl4ai_user_data

# Set the entrypoint to run the batch application
ENTRYPOINT ["python", "app.py"]