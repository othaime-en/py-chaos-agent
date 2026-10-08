"""
Standalone API server for Py-Chaos-Agent.

Run this to start the FastAPI server:
    python -m src.api_server

Binds to 127.0.0.1 by default and requires an API token (see src/auth.py).
To listen on other interfaces, set a token and opt in explicitly:
    CHAOS_API_TOKEN=$(openssl rand -hex 32) \
        python -m src.api_server --host 0.0.0.0 --port 9000
"""

import uvicorn
import argparse
import os
import sys
from . import auth
from .logging_config import setup_logging, get_logger
from .metrics import start_metrics_server

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Py-Chaos-Agent API Server")
    parser.add_argument(
        "--host",
        default=os.environ.get("CHAOS_API_HOST", "127.0.0.1"),
        help=(
            "Host to bind the API server. Default: 127.0.0.1, or the "
            "CHAOS_API_HOST environment variable. Binding to a non-loopback "
            "address requires an API token."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="Port to bind the API server (default: 9000)",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=8000,
        help="Port for Prometheus metrics (default: 8000)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )

    parser.add_argument(
        "--insecure-no-auth",
        action="store_true",
        help=(
            "Disable API authentication. Only allowed when bound to a "
            "loopback address. For local development only."
        ),
    )

    args = parser.parse_args()

    # Setup logging
    logging_config = {
        "level": args.log_level,
        "format": "text",
        "console": {"enabled": True},
        "file": {"enabled": False},
    }
    setup_logging(logging_config)

    # Refuse unsafe configurations before opening any ports.
    auth.validate_startup(args.host, args.insecure_no_auth)
    if args.insecure_no_auth:
        # Propagate to the app (also covers uvicorn --reload subprocesses).
        os.environ[auth.AUTH_DISABLED_ENV] = "true"

    logger.info(
        "Starting Py-Chaos-Agent API Server",
        extra={
            "api_host": args.host,
            "api_port": args.port,
            "metrics_port": args.metrics_port,
        },
    )

    # Start metrics server
    try:
        start_metrics_server(port=args.metrics_port)
        logger.info(f"Metrics server started on port {args.metrics_port}")
    except Exception as e:
        logger.error(f"Failed to start metrics server: {e}")
        sys.exit(1)

    # Start API server
    uvicorn.run(
        "src.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level.lower(),
    )


if __name__ == "__main__":
    main()
