FROM python:3.12-slim

WORKDIR /app

# Install system deps for camoufox/playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget git && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Create data directory
RUN mkdir -p unified/data

# Expose port
EXPOSE 1430

# Run unified proxy
CMD ["python", "-m", "unified.main"]
