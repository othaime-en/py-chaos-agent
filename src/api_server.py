"""
Standalone API server for Py-Chaos-Agent.

Run this to start the FastAPI server:
    python -m src.api_server

Binds to 127.0.0.1 by default and requires an API token (see src/auth.py).
To listen on other interfaces, set a token and opt in explicitly:
    CHAOS_API_TOKEN=$(openssl rand -hex 32) \
        python -m src.api_server --host 0.0.0.0 --port 9000

Add TLS (see src/tls.py) so the token does not cross the network in clear text:
    python -m src.api_server --host 0.0.0.0 \
        --tls-cert fullchain.pem --tls-key privkey.pem
"""

import uvicorn
import argparse
import os
import signal
from . import auth, targets, tls
from .audit import AuditError, audit
from .lifecycle import lifecycle
from .logging_config import setup_logging, get_logger
from .metrics import MetricsServerError, metrics_port_from_env, start_metrics_server

logger = get_logger(__name__)


class ChaosServer(uvicorn.Server):
    """
    uvicorn server that cleans up the moment a shutdown signal arrives.

    uvicorn's graceful shutdown waits for running background tasks, and an
    injection can run for minutes. Without this, a SIGTERM during an injection
    would wait out the injection, and Kubernetes would SIGKILL the pod after its
    grace period with the tc rule still applied. Here the signal first wakes
    every injection and removes its effect, then uvicorn proceeds as usual.
    """

    def handle_exit(self, sig, frame):
        lifecycle.shutdown(f"signal {signal.Signals(sig).name}")
        super().handle_exit(sig, frame)


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
        default=None,
        help=(
            "Port for Prometheus metrics. Default: the CHAOS_METRICS_PORT "
            "environment variable, or 8000."
        ),
    )
    parser.add_argument(
        "--tls-cert",
        default=None,
        help="PEM certificate (chain) to serve HTTPS. Default: CHAOS_TLS_CERT_FILE.",
    )
    parser.add_argument(
        "--tls-key",
        default=None,
        help="PEM private key for --tls-cert. Default: CHAOS_TLS_KEY_FILE.",
    )
    parser.add_argument(
        "--tls-client-ca",
        default=None,
        help=(
            "Require clients to present a certificate signed by this CA (mutual "
            "TLS). Default: CHAOS_TLS_CLIENT_CA_FILE."
        ),
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

    try:
        tls_settings = tls.settings_from_env(
            args.tls_cert, args.tls_key, args.tls_client_ca
        )
    except tls.TlsError as e:
        raise SystemExit(f"Refusing to start: {e}")
    tls.validate_startup(args.host, tls_settings)
    if tls_settings is not None:
        # Propagate flags to the environment so the metrics server uses the same
        # certificate (and uvicorn --reload subprocesses see it).
        os.environ[tls.CERT_ENV] = tls_settings.cert_file
        os.environ[tls.KEY_ENV] = tls_settings.key_file
        if tls_settings.client_ca_file:
            os.environ[tls.CLIENT_CA_ENV] = tls_settings.client_ca_file

    # A requested audit file that cannot be written stops startup too.
    try:
        audit.configure_from_env()
    except AuditError as e:
        raise SystemExit(f"Refusing to start: {e}")

    # Labels that are set but invalid would make every selector fail closed
    # without saying why: stop here and say it instead.
    try:
        labels = targets.agent_labels.validate_startup()
    except targets.TargetLabelsError as e:
        raise SystemExit(f"Refusing to start: {e}")

    logger.info(
        "Starting Py-Chaos-Agent API Server",
        extra={
            "api_host": args.host,
            "api_port": args.port,
            "tls": tls_settings is not None,
            "metrics_port": args.metrics_port,  # None: from CHAOS_METRICS_PORT or 8000
            "target_labels": labels,
        },
    )

    # Start metrics server. It binds before returning, so a port already in use
    # stops startup here with a clear message.
    try:
        metrics_port = (
            args.metrics_port
            if args.metrics_port is not None
            else metrics_port_from_env()
        )
        start_metrics_server(port=metrics_port)
        logger.info(f"Metrics server started on port {metrics_port}")
    except MetricsServerError as e:
        raise SystemExit(f"Refusing to start: {e}")

    # Backstop: run cleanups at interpreter exit even if no signal handler ran.
    lifecycle.install_atexit()

    # Start API server
    if args.reload:
        # Development only: uvicorn's reloader supervises a child process and
        # bypasses ChaosServer. The API lifespan and atexit still run cleanups.
        uvicorn.run(
            "src.api:app",
            host=args.host,
            port=args.port,
            reload=True,
            log_level=args.log_level.lower(),
            **tls.uvicorn_options(tls_settings),
        )
    else:
        server = ChaosServer(
            uvicorn.Config(
                "src.api:app",
                host=args.host,
                port=args.port,
                log_level=args.log_level.lower(),
                **tls.uvicorn_options(tls_settings),
            )
        )
        server.run()


if __name__ == "__main__":
    main()
