"""
FastAPI interface for Py-Chaos-Agent control.

Provides programmatic control over chaos injections through REST API.
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from enum import Enum
import threading
import time
import random
from datetime import datetime
from contextlib import asynccontextmanager

from .config import Config, load_config
from .failures.cpu import inject_cpu
from .failures.memory import inject_memory
from .failures.process import inject_process
from .failures.network import inject_network
from .logging_config import get_logger, set_correlation_id
from .metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE

logger = get_logger(__name__)


# Global state with proper typing
class AgentState:
    """Global state for the chaos agent."""

    enabled: bool = False
    config: Optional[Config] = None
    agent_thread: Optional[threading.Thread] = None
    stop_event: threading.Event = threading.Event()
    start_time: Optional[float] = None


agent_state = AgentState()


def get_config() -> Config:
    """Get config with type safety."""
    if agent_state.config is None:
        raise HTTPException(status_code=500, detail="Configuration not loaded")
    return agent_state.config


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for startup and shutdown."""
    # Startup
    try:
        config = load_config()
        agent_state.config = config
        logger.info("API started, configuration loaded")
    except Exception as e:
        logger.error(f"Failed to load config on startup: {e}")
        agent_state.config = None

    yield

    # Shutdown (optional cleanup)
    if agent_state.enabled:
        logger.info("Shutting down agent on API shutdown")
        agent_state.stop_event.set()


app = FastAPI(
    title="Py-Chaos-Agent API",
    description="REST API for controlling chaos engineering experiments",
    version="1.0.0",
    lifespan=lifespan,
)


class FailureType(str, Enum):
    CPU = "cpu"
    MEMORY = "memory"
    PROCESS = "process"
    NETWORK = "network"


class AgentStatus(BaseModel):
    enabled: bool
    uptime_seconds: Optional[float] = None
    config_loaded: bool
    dry_run: bool
    interval_seconds: int
    enabled_failures: List[str]


class ManualInjectionRequest(BaseModel):
    failure_type: FailureType
    dry_run: bool = False
    config: Optional[Dict[str, Any]] = None


class ConfigUpdateRequest(BaseModel):
    interval_seconds: Optional[int] = Field(None, ge=1, le=300)
    dry_run: Optional[bool] = None
    failures: Optional[Dict[str, Dict[str, Any]]] = None


class FailureConfigResponse(BaseModel):
    failure_type: str
    enabled: bool
    probability: float
    config: Dict[str, Any]


# ============================================================================
# Agent Control
# ============================================================================


def run_agent_loop():
    """
    Background agent loop - similar to main() in agent.py but controllable.

    FIXED: Now gets fresh config each iteration to pick up runtime updates.
    """
    logger.info("API-controlled agent loop starting")
    start_time = time.time()
    agent_state.start_time = start_time
    iteration = 0

    while not agent_state.stop_event.is_set():
        try:
            iteration += 1
            correlation_id = f"api-iter-{iteration}-{int(time.time())}"
            set_correlation_id(correlation_id)

            config = get_config()

            for name, cfg in config.failures.items():
                if agent_state.stop_event.is_set():
                    break

                if not cfg["enabled"]:
                    continue

                probability = cfg["probability"]
                if random.random() > probability:
                    continue

                logger.info(
                    f"Injecting {name} failure",
                    extra={"failure_type": name, "iteration": iteration},
                )

                try:
                    if name == "cpu":
                        inject_cpu(cfg, dry_run=config.agent.dry_run)
                    elif name == "memory":
                        inject_memory(cfg, dry_run=config.agent.dry_run)
                    elif name == "process":
                        inject_process(cfg, dry_run=config.agent.dry_run)
                    elif name == "network":
                        inject_network(cfg, dry_run=config.agent.dry_run)
                except Exception as e:
                    logger.error(
                        f"Failure injection error: {e}",
                        exc_info=True,
                        extra={"failure_type": name},
                    )

            # Wait for interval or stop signal
            # Also get fresh config in case interval changed
            agent_state.stop_event.wait(config.agent.interval_seconds)

        except Exception as e:
            logger.error(f"Error in agent loop: {e}", exc_info=True)
            try:
                config = get_config()
                agent_state.stop_event.wait(config.agent.interval_seconds)
            except Exception:
                agent_state.stop_event.wait(10)  # Fallback interval

    logger.info("API-controlled agent loop stopped")
    agent_state.enabled = False


# ============================================================================
# API Endpoints
# ============================================================================


@app.get("/", tags=["General"])
async def root():
    """API root - health check."""
    return {
        "service": "py-chaos-agent",
        "version": "1.0.0",
        "status": "running",
        "agent_enabled": agent_state.enabled,
    }


@app.get("/health", tags=["General"])
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "config_loaded": agent_state.config is not None,
        "agent_enabled": agent_state.enabled,
    }


@app.get("/status", response_model=AgentStatus, tags=["Agent Control"])
async def get_status():
    """Get current agent status."""
    config = get_config()
    uptime = None
    if agent_state.enabled and agent_state.start_time is not None:
        uptime = time.time() - agent_state.start_time

    enabled_failures = [name for name, cfg in config.failures.items() if cfg["enabled"]]

    return AgentStatus(
        enabled=agent_state.enabled,
        uptime_seconds=uptime,
        config_loaded=True,
        dry_run=config.agent.dry_run,
        interval_seconds=config.agent.interval_seconds,
        enabled_failures=enabled_failures,
    )


@app.post("/agent/start", tags=["Agent Control"])
async def start_agent():
    """Start the chaos agent loop."""
    if agent_state.enabled:
        raise HTTPException(status_code=400, detail="Agent already running")

    if agent_state.config is None:
        raise HTTPException(status_code=500, detail="Configuration not loaded")

    logger.info("Starting chaos agent via API")
    agent_state.stop_event.clear()
    agent_state.enabled = True
    agent_state.agent_thread = threading.Thread(target=run_agent_loop, daemon=True)
    agent_state.agent_thread.start()

    return {"status": "started", "message": "Chaos agent started successfully"}


@app.post("/agent/stop", tags=["Agent Control"])
async def stop_agent():
    """Stop the chaos agent loop."""
    if not agent_state.enabled:
        raise HTTPException(status_code=400, detail="Agent not running")

    logger.info("Stopping chaos agent via API")
    agent_state.stop_event.set()

    # Wait briefly for thread to stop
    if agent_state.agent_thread:
        agent_state.agent_thread.join(timeout=5)

    agent_state.enabled = False

    return {"status": "stopped", "message": "Chaos agent stopped successfully"}


@app.post("/agent/restart", tags=["Agent Control"])
async def restart_agent():
    """Restart the chaos agent loop."""
    if agent_state.enabled:
        await stop_agent()
        time.sleep(1)

    return await start_agent()


# ============================================================================
# Manual Injections
# ============================================================================


@app.post("/inject/manual", tags=["Injections"])
async def manual_injection(
    request: ManualInjectionRequest, background_tasks: BackgroundTasks
):
    """
    Manually trigger a single failure injection.

    This bypasses probability checks and injects immediately.
    """
    config = get_config()
    failure_type = request.failure_type.value

    if failure_type not in config.failures:
        raise HTTPException(
            status_code=404, detail=f"Failure type '{failure_type}' not found"
        )

    # Use provided config or fall back to loaded config
    failure_config = request.config if request.config else config.failures[failure_type]

    logger.info(
        f"Manual injection requested: {failure_type}",
        extra={"failure_type": failure_type, "dry_run": request.dry_run},
    )

    def inject():
        try:
            if failure_type == "cpu":
                inject_cpu(failure_config, dry_run=request.dry_run)
            elif failure_type == "memory":
                inject_memory(failure_config, dry_run=request.dry_run)
            elif failure_type == "process":
                inject_process(failure_config, dry_run=request.dry_run)
            elif failure_type == "network":
                inject_network(failure_config, dry_run=request.dry_run)
        except Exception as e:
            logger.error(f"Manual injection failed: {e}", exc_info=True)

    # Run in background to not block API
    background_tasks.add_task(inject)

    return {
        "status": "injecting",
        "failure_type": failure_type,
        "dry_run": request.dry_run,
        "message": f"Manual {failure_type} injection started",
    }


# ============================================================================
# Configuration Management
# ============================================================================


@app.get("/config", tags=["Configuration"])
async def get_config_endpoint():
    """Get current agent configuration."""
    config = get_config()

    return {
        "agent": {
            "interval_seconds": config.agent.interval_seconds,
            "dry_run": config.agent.dry_run,
        },
        "failures": config.failures,
    }


@app.get(
    "/config/failures/{failure_type}",
    response_model=FailureConfigResponse,
    tags=["Configuration"],
)
async def get_failure_config(failure_type: FailureType):
    """Get configuration for a specific failure type."""
    config = get_config()
    failure_name = failure_type.value

    if failure_name not in config.failures:
        raise HTTPException(
            status_code=404, detail=f"Failure type '{failure_name}' not found"
        )

    failure_config = config.failures[failure_name]

    return FailureConfigResponse(
        failure_type=failure_name,
        enabled=failure_config.get("enabled", False),
        probability=failure_config.get("probability", 0.0),
        config=failure_config,
    )


@app.patch("/config", tags=["Configuration"])
async def update_config(request: ConfigUpdateRequest):
    """
    Update agent configuration dynamically.

    Changes take effect immediately if agent is running.
    """
    config = get_config()
    changes: Dict[str, Any] = {}

    if request.interval_seconds is not None:
        config.agent.interval_seconds = request.interval_seconds
        changes["interval_seconds"] = request.interval_seconds

    if request.dry_run is not None:
        config.agent.dry_run = request.dry_run
        changes["dry_run"] = request.dry_run

    if request.failures is not None:
        for failure_type, failure_config in request.failures.items():
            if failure_type in config.failures:
                config.failures[failure_type].update(failure_config)
                changes[f"failures.{failure_type}"] = failure_config

    logger.info("Configuration updated via API", extra={"changes": changes})

    return {
        "status": "updated",
        "message": "Configuration updated successfully",
        "changes": changes,
        "note": (
            "Changes take effect immediately"
            if agent_state.enabled
            else "Changes will take effect when agent starts"
        ),
    }


@app.patch("/config/failures/{failure_type}", tags=["Configuration"])
async def update_failure_config(
    failure_type: FailureType, config_update: Dict[str, Any]
):
    """Update configuration for a specific failure type."""
    config = get_config()
    failure_name = failure_type.value

    if failure_name not in config.failures:
        raise HTTPException(
            status_code=404, detail=f"Failure type '{failure_name}' not found"
        )

    config.failures[failure_name].update(config_update)

    logger.info(
        f"Updated {failure_name} configuration",
        extra={"failure_type": failure_name, "updates": config_update},
    )

    return {
        "status": "updated",
        "failure_type": failure_name,
        "config": config.failures[failure_name],
    }


@app.post("/config/reload", tags=["Configuration"])
async def reload_config():
    """Reload configuration from config.yaml file."""
    try:
        config = load_config()
        agent_state.config = config
        logger.info("Configuration reloaded from file")

        return {
            "status": "reloaded",
            "message": "Configuration reloaded successfully from config.yaml",
            "note": (
                "Agent must be restarted for changes to take full effect"
                if agent_state.enabled
                else None
            ),
        }
    except Exception as e:
        logger.error(f"Failed to reload config: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to reload config: {str(e)}"
        )


# ============================================================================
# Metrics & Monitoring
# ============================================================================


@app.get("/metrics/summary", tags=["Metrics"])
async def get_metrics_summary():
    """Get summary of chaos injection metrics."""

    # Collect metrics for all failure types
    summary = {}

    for failure_type in ["cpu", "memory", "process", "network"]:
        success = INJECTIONS_TOTAL.labels(
            failure_type=failure_type, status="success"
        )._value.get()
        failed = INJECTIONS_TOTAL.labels(
            failure_type=failure_type, status="failed"
        )._value.get()
        skipped = INJECTIONS_TOTAL.labels(
            failure_type=failure_type, status="skipped"
        )._value.get()
        active = INJECTION_ACTIVE.labels(failure_type=failure_type)._value.get()

        summary[failure_type] = {
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "active": active,
            "total": success + failed + skipped,
        }

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "metrics": summary,
    }


@app.post("/metrics/reset", tags=["Metrics"])
async def reset_metrics():
    """Reset all metrics counters (useful for testing)."""
    logger.warning("Metrics reset requested via API")

    INJECTIONS_TOTAL._metrics.clear()
    INJECTION_ACTIVE._metrics.clear()

    return {
        "status": "reset",
        "message": "All metrics have been reset",
    }
