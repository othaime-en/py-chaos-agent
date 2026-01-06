# src/config.py
import yaml
from dataclasses import dataclass
from typing import Dict, Any


@dataclass
class FailureConfig:
    enabled: bool
    probability: float
    duration_seconds: int = 0


@dataclass
class AgentConfig:
    interval_seconds: int
    dry_run: bool


@dataclass
class ChaosConfig:
    agent: AgentConfig
    failures: Dict[str, FailureConfig]


def load_config(path: str = "config.yaml") -> ChaosConfig:
    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    agent_cfg = AgentConfig(
        interval_seconds=raw["agent"]["interval_seconds"],
        dry_run=raw["agent"].get("dry_run", False),
    )

    failures = {}
    for name, cfg in raw["failures"].items():
        base = FailureConfig(
            enabled=cfg["enabled"],
            probability=cfg["probability"],
            duration_seconds=cfg.get("duration_seconds", 0),
        )
        # Merge extra fields (like mb, delay_ms) into a dict for flexibility
        extra = {
            k: v
            for k, v in cfg.items()
            if k not in ["enabled", "probability", "duration_seconds"]
        }
        failures[name] = {**base.__dict__, **extra}

    return ChaosConfig(agent=agent_cfg, failures=failures)
