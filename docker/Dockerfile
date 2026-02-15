FROM python:3.10-slim

# Install tc (for network latency) and other utilities
RUN apt-get update && apt-get install -y \
    iproute2 \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Create app directory and logs directory
WORKDIR /app
RUN mkdir -p /var/log/chaos-agent

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config.yaml .

# Run as root for chaos operations
USER root

# Set default log level (can be overridden)
ENV LOG_LEVEL=INFO
ENV JSON_LOGS=true

# Expose API port (9000) and metrics port (8000)
EXPOSE 9000 8000

# Run API server by default
# To run standalone agent instead: docker run <image> python -m src.agent
CMD ["python", "-u", "-m", "src.api_server", "--host", "0.0.0.0", "--port", "9000"]