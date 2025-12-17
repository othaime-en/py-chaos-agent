FROM python:3.10-slim

# Install tc (for network latency) and other utilities
RUN apt-get update && apt-get install -y \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements and install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY config.yaml .

# Run as non-root for security (but still need CAP_NET_ADMIN for tc)
RUN useradd -m appuser
USER appuser

# Entry point
CMD ["python", "-u", "src/agent.py"]