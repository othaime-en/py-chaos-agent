import time
import random
import importlib
import signal
import sys
from .config import load_config
from .metrics import start_metrics_server
from .failures.network import cleanup_network_rules

FAILURE_MODULES = {
    "cpu": ".failures.cpu",
    "memory": ".failures.memory",
    "process": ".failures.process",
    "network": ".failures.network",
}

# Track configured interfaces for cleanup
_configured_interfaces: set[str] = set()


def cleanup_on_exit():
    """Clean up any active network rules on shutdown."""
    print("[AGENT] Performing cleanup on shutdown...")
    for interface in _configured_interfaces:
        success, error = cleanup_network_rules(interface)
        if success:
            print(f"[AGENT] Cleaned up network rules on {interface}")
        else:
            print(f"[AGENT] Warning: Failed to cleanup {interface}: {error}")


def signal_handler(sig, frame):
    """Handle shutdown signals gracefully."""
    print(f"\n[AGENT] Received signal {sig}, shutting down...")
    cleanup_on_exit()
    sys.exit(0)


def main():
    print("[AGENT] Starting Py-Chaos-Agent...")
    config = load_config()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    print("[AGENT] Performing startup cleanup...")
    if "network" in config.failures and config.failures["network"]["enabled"]:
        interface = config.failures["network"].get("interface", "eth0")
        _configured_interfaces.add(interface)
        cleanup_network_rules(interface)
        print(f"[AGENT] Cleaned up any existing network rules on {interface}")

    start_metrics_server()

    print(
        f"[CONFIG] Interval: {config.agent.interval_seconds}s | Dry Run: {config.agent.dry_run}"
    )

    while True:
        for name, cfg in config.failures.items():
            if not cfg["enabled"]:
                continue
            if random.random() > cfg["probability"]:
                continue

            module = importlib.import_module(FAILURE_MODULES[name], package=__package__)
            inject_func = getattr(module, f"inject_{name}")
            inject_func(cfg, dry_run=config.agent.dry_run)

        time.sleep(config.agent.interval_seconds)


if __name__ == "__main__":
    main()
