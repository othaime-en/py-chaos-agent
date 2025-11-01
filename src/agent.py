import time
import random
import importlib
from config import load_config
from metrics import start_metrics_server

FAILURE_MODULES = {
    'cpu': 'failures.cpu',
    'memory': 'failures.memory',
    'process': 'failures.process',
    'network': 'failures.network'
}

def main():
    print("[AGENT] Starting Py-Chaos-Agent...")
    config = load_config()
    start_metrics_server()

    print(f"[CONFIG] Interval: {config.agent.interval_seconds}s | Dry Run: {config.agent.dry_run}")

    while True:
        for name, cfg in config.failures.items():
            if not cfg['enabled']:
                continue
            if random.random() > cfg['probability']:
                continue

            module = importlib.import_module(FAILURE_MODULES[name])
            inject_func = getattr(module, f"inject_{name}")
            inject_func(cfg, dry_run=config.agent.dry_run)

        time.sleep(config.agent.interval_seconds)

if __name__ == "__main__":
    main()