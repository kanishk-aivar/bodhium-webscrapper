FROM mcr.microsoft.com/playwright/python:v1.53.0-noble

# Set working directory
WORKDIR /app

# Set environment variables early
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8
ENV DEBIAN_FRONTEND=noninteractive

# Update system packages and install essential tools + PostgreSQL client
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    wget \
    gnupg \
    ca-certificates \
    zip \
    unzip \
    libpq-dev \
    postgresql-client \
    build-essential \
    python3-dev \
    libffi-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies with optimizations
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Install additional packages for database connectivity
RUN pip install --no-cache-dir google-generativeai crawl4ai pydantic python-dotenv boto3 psycopg2-binary

# CRITICAL: Install browsers explicitly with system dependencies and verify installation
RUN playwright install --with-deps chromium && \
    playwright install-deps && \
    # Verify browser installation
    python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); browser = p.chromium.launch(); browser.close(); p.stop()" || \
    (echo "Browser installation failed" && exit 1)

# Create directories with proper permissions and ownership
RUN mkdir -p /tmp/.crawl4ai /tmp/.crawl4ai_cache /tmp/.crawl4ai_user_data /tmp/screenshots /tmp/crawl_output \
             /app/data /app/logs /app/csv_files /tmp/.cache/ms-playwright && \
    chmod 777 /tmp/.crawl4ai /tmp/.crawl4ai_cache /tmp/.crawl4ai_user_data /tmp/screenshots /tmp/crawl_output /tmp/.cache && \
    chown -R pwuser:pwuser /tmp/.crawl4ai /tmp/.crawl4ai_cache /tmp/.crawl4ai_user_data /tmp/screenshots /tmp/crawl_output \
                           /app/data /app/logs /app/csv_files /tmp/.cache

# Use the existing pwuser from Playwright image for security
USER pwuser

# Copy the application files with proper ownership
COPY --chown=pwuser:pwuser . .

# Set crawl4ai specific environment variables
ENV CRAWL4AI_DB_PATH=/tmp/.crawl4ai
ENV CRAWL4AI_CACHE_DIR=/tmp/.crawl4ai_cache
ENV CRAWL4AI_BASE_DIRECTORY=/tmp
ENV HOME=/tmp

# Set Playwright environment variables
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0

# Set AWS region for RDS access (non-sensitive)
ENV AWS_DEFAULT_REGION=us-east-1

# Set S3 configuration (non-sensitive)
ENV S3_PATH=s3://bodhium/data/

# PostgreSQL RDS connection settings (NON-SENSITIVE ONLY)
ENV DB_HOST=bodhium-dev.cmhacmc4ox7v.us-east-1.rds.amazonaws.com
ENV DB_NAME=bodhium-dev
ENV DB_USER=postgres
ENV DB_PORT=5432
ENV DB_SSLMODE=require

# Connection pool settings
ENV DB_MIN_CONNECTIONS=2
ENV DB_MAX_CONNECTIONS=20
ENV DB_CONNECTION_TIMEOUT=30

# Pre-create Chrome user data directory with proper permissions
RUN mkdir -p /tmp/.crawl4ai_user_data/Default && \
    chmod -R 755 /tmp/.crawl4ai_user_data

# Expose port (optional)
EXPOSE 8000

# Health check for database connectivity
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import psycopg2; psycopg2.connect(host='\${DB_HOST}', database='\${DB_NAME}', user='\${DB_USER}', password='\${DB_PASSWORD}', port='\${DB_PORT}', sslmode='\${DB_SSLMODE}', connect_timeout=5).close()" || exit 1

# Set the entrypoint to run the application
ENTRYPOINT ["python", "app.py"]