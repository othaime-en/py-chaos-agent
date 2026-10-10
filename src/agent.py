import time
import random
import importlib
import signal
import sys
import uuid
import logging
from .config import load_config, validate_config
from .metrics import start_metrics_server
from .resources import apply_settings_and_report
from .failures.network import reconcile_stale_rules
from .lifecycle import lifecycle
from .logging_config import (
    setup_logging,
    get_logger,
    set_correlation_id,
    log_failure_injection,
)

# Logger will be initialized after config is loaded
logger: logging.Logger

FAILURE_MODULES = {
    "cpu": ".failures.cpu",
    "memory": ".failures.memory",
    "process": ".failures.process",
    "network": ".failures.network",
}


def cleanup_on_exit():
    """Run all registered cleanups (network rules, and anything else)."""
    logger.info("Performing cleanup on shutdown")
    lifecycle.run_cleanups()


def signal_handler(sig, frame):
    """
    Handle shutdown signals: wake and abort any in-flight injection, run the
    cleanups (so a tc rule is removed immediately), then exit.
    """
    name = signal.Signals(sig).name
    logger.info("Shutdown signal received", extra={"signal": sig, "signal_name": name})
    lifecycle.shutdown(f"signal {name}")
    logger.info("Agent shutdown complete")
    sys.exit(0)


def main():
    global logger

    # Load config first
    try:
        config = load_config()
    except Exception as e:
        # Can't use logger yet, fall back to print
        print(f"CRITICAL: Failed to load configuration: {e}", file=sys.stderr)
        sys.exit(1)

    # Setup logging from config.yaml
    logging_config = config.raw_config.get("logging", {})
    setup_logging(logging_config)

    # Now we can get the logger
    logger = get_logger(__name__)

    logger.info("Py-Chaos-Agent starting", extra={"version": "1.0.0"})

    for warning in validate_config(config):
        logger.warning("Configuration warning", extra={"warning": warning})

    apply_settings_and_report(config.safety)
    logger.info(
        "Configuration loaded successfully",
        extra={
            "interval_seconds": config.agent.interval_seconds,
            "dry_run": config.agent.dry_run,
            "enabled_failures": [
                name for name, cfg in config.failures.items() if cfg["enabled"]
            ],
        },
    )

    # Register signal handlers for graceful shutdown, and run cleanups at
    # interpreter exit as a backstop for exits that bypass the handlers.
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    lifecycle.install_atexit()
    logger.debug("Signal handlers registered")

    # Startup repair: a previous run killed by SIGKILL or the OOM killer cannot
    # clean up after itself, so remove any netem rule it left on the interface
    # we manage. Runs whether or not the network failure is currently enabled.
    logger.info("Performing startup cleanup")
    if "network" in config.failures:
        interface = config.failures["network"].get("interface", "eth0")
        repaired = reconcile_stale_rules([interface])
        logger.info(
            "Startup network cleanup completed",
            extra={"interface": interface, "stale_rule_removed": bool(repaired)},
        )

    # Start metrics server
    try:
        start_metrics_server()
        logger.info("Metrics server started successfully")
    except Exception as e:
        logger.error(
            "Failed to start metrics server", exc_info=True, extra={"error": str(e)}
        )

    logger.info(
        "Agent initialized and ready",
        extra={
            "interval_seconds": config.agent.interval_seconds,
            "dry_run_mode": config.agent.dry_run,
        },
    )

    # Main loop
    iteration = 0
    while not lifecycle.is_shutting_down():
        try:
            iteration += 1
            # Create correlation ID for this iteration
            correlation_id = f"iter-{iteration}-{uuid.uuid4().hex[:8]}"
            set_correlation_id(correlation_id)

            logger.debug(
                "Starting chaos iteration",
                extra={"iteration": iteration, "correlation_id": correlation_id},
            )

            injections_attempted = 0
            injections_executed = 0

            for name, cfg in config.failures.items():
                if not cfg["enabled"]:
                    logger.debug(
                        "Failure type disabled, skipping", extra={"failure_type": name}
                    )
                    continue

                probability = cfg["probability"]
                roll = random.random()

                if roll > probability:
                    logger.debug(
                        "Probability check failed, skipping injection",
                        extra={
                            "failure_type": name,
                            "probability": probability,
                            "roll": round(roll, 3),
                        },
                    )
                    continue

                injections_attempted += 1

                logger.info(
                    "Probability check passed, injecting failure",
                    extra={
                        "failure_type": name,
                        "probability": probability,
                        "roll": round(roll, 3),
                        "dry_run": config.agent.dry_run,
                    },
                )

                try:
                    module = importlib.import_module(
                        FAILURE_MODULES[name], package=__package__
                    )
                    inject_func = getattr(module, f"inject_{name}")

                    log_failure_injection(
                        logger,
                        failure_type=name,
                        action="start",
                        status="executing",
                        config=cfg,
                    )

                    inject_func(cfg, dry_run=config.agent.dry_run)
                    injections_executed += 1

                    log_failure_injection(
                        logger, failure_type=name, action="complete", status="success"
                    )

                except Exception as e:
                    logger.error(
                        "Failure injection raised exception",
                        exc_info=True,
                        extra={
                            "failure_type": name,
                            "error": str(e),
                            "error_type": type(e).__name__,
                        },
                    )

                    log_failure_injection(
                        logger,
                        failure_type=name,
                        action="failed",
                        status="error",
                        error=str(e),
                    )

            logger.debug(
                "Chaos iteration complete",
                extra={
                    "iteration": iteration,
                    "injections_attempted": injections_attempted,
                    "injections_executed": injections_executed,
                },
            )

            logger.debug(f"Sleeping for {config.agent.interval_seconds} seconds")
            time.sleep(config.agent.interval_seconds)

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
            break
        except Exception as e:
            logger.critical(
                "Unexpected error in main loop",
                exc_info=True,
                extra={
                    "iteration": iteration,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            # Continue running despite errors
            time.sleep(config.agent.interval_seconds)


if __name__ == "__main__":
    main()
