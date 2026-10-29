import signal
import sys
import logging
from .config import load_config, validate_config
from .metrics import MetricsServerError, metrics_port_from_env, start_metrics_server
from .resources import apply_settings_and_report
from .failures.network import reconcile_stale_rules
from .engine import ChaosEngine, snapshot_settings
from .kill_switch import KillSwitch
from .lifecycle import lifecycle
from .logging_config import setup_logging, get_logger

# Logger will be initialized after config is loaded
logger: logging.Logger


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

    # Kill switch: pause chaos while the target is unhealthy, resume when it
    # recovers. The standalone agent runs chaos for its whole life, so it is
    # always "armed".
    kill_switch = KillSwitch(config.kill_switch)
    if config.kill_switch.enabled:
        kill_switch.start_monitoring(
            lambda: lifecycle.abort_active("kill switch triggered")
        )
    else:
        logger.warning(
            "Kill switch is disabled: chaos will run without automatic protection"
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

    # Start the metrics server. It binds before returning, so a port that is
    # already in use fails here, loudly. A chaos agent nobody can observe is not
    # worth running, so exit instead of carrying on blind.
    try:
        metrics_port = metrics_port_from_env()
        start_metrics_server(metrics_port)
        logger.info("Metrics server started", extra={"port": metrics_port})
    except MetricsServerError as e:
        logger.critical("Cannot start the metrics server", extra={"error": str(e)})
        sys.exit(1)

    logger.info(
        "Agent initialized and ready",
        extra={
            "interval_seconds": config.agent.interval_seconds,
            "dry_run_mode": config.agent.dry_run,
        },
    )

    # The same engine drives the API mode. The config never changes in this
    # mode, so each tick just snapshots it.
    engine = ChaosEngine(
        settings_provider=lambda: snapshot_settings(config),
        kill_switch_provider=lambda: (
            kill_switch if config.kill_switch.enabled else None
        ),
        name="chaos-agent",
    )
    engine.start()

    try:
        # Block here. SIGTERM and SIGINT run signal_handler, which cleans up and
        # raises SystemExit; the engine thread is a daemon, so it goes with us.
        while engine.is_running:
            engine.join(timeout=1.0)
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
