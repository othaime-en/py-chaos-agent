"""
FastAPI interface for Py-Chaos-Agent control.

Provides programmatic control over chaos injections through REST API.
"""

from fastapi import Depends, FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, Dict, Any, List
from enum import Enum
import os
import threading
import time
import random
from datetime import datetime
from contextlib import asynccontextmanager

from .auth import auth_disabled, get_expected_token, require_auth
from . import limits
from .config import Config, load_config, validate_config
from .resources import apply_settings_and_report, governor
from .schemas import (
    ConfigValidationError,
    merge_failure_config,
)
from .failures.cpu import inject_cpu
from .failures.memory import inject_memory
from .failures.process import inject_process
from .failures.network import inject_network
from .logging_config import get_logger, set_correlation_id
from .metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE
from .kill_switch import (
    KillSwitch,
    PRODUCTION_CONFIG,
    wrap_injection_with_context,
    CircuitState,
)

logger = get_logger(__name__)


# Global state with proper typing
class AgentState:
    """Global state for the chaos agent."""

    enabled: bool = False
    config: Optional[Config] = None
    agent_thread: Optional[threading.Thread] = None
    stop_event: threading.Event = threading.Event()
    start_time: Optional[float] = None
    kill_switch: Optional[KillSwitch] = None


agent_state = AgentState()


def get_config() -> Config:
    """Get config with type safety."""
    if agent_state.config is None:
        raise HTTPException(status_code=500, detail="Configuration not loaded")
    return agent_state.config


def _validation_error(exc: ConfigValidationError) -> HTTPException:
    """
    Turn a ConfigValidationError into a 422 using FastAPI's own error shape
    (a list of {loc, msg}), so clients parse one format for all validation
    failures. Pydantic's raw ``input`` field is not included.
    """
    return HTTPException(status_code=422, detail=exc.errors)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for startup and shutdown."""
    # Startup
    try:
        config = load_config()
        agent_state.config = config
        logger.info("API started, configuration loaded")
        for warning in validate_config(config):
            logger.warning("Configuration warning", extra={"warning": warning})
        apply_settings_and_report(config.safety)
    except Exception as e:
        logger.error(f"Failed to load config on startup: {e}")
        agent_state.config = None

    # Surface auth misconfiguration immediately instead of on first request.
    try:
        if not auth_disabled() and get_expected_token() is None:
            logger.error(
                "No API token configured: all protected endpoints will return 503"
            )
    except ValueError as e:
        logger.error(f"Invalid API token configuration: {e}")

    yield

    # Shutdown (optional cleanup)
    if agent_state.enabled:
        logger.info("Shutting down agent on API shutdown")
        agent_state.stop_event.set()


# Interactive docs expose the full API surface and are served outside the
# app-level auth dependency, so they are off unless explicitly enabled.
_docs_enabled = os.environ.get("CHAOS_API_ENABLE_DOCS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

app = FastAPI(
    title="Py-Chaos-Agent API",
    description="REST API for controlling chaos engineering experiments",
    version="1.0.0",
    lifespan=lifespan,
    # Secure by default: every route requires a bearer token except the
    # liveness endpoints in src.auth.PUBLIC_PATHS.
    dependencies=[Depends(require_auth)],
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
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
    """
    ``config`` overrides are merged onto the stored config for that failure
    type and the merged result is validated, so a partial override such as
    ``{"duration_seconds": 3}`` is enough.
    """

    model_config = ConfigDict(extra="forbid")

    failure_type: FailureType
    dry_run: bool = Field(False, strict=True)
    config: Optional[Dict[str, Any]] = None


class ConfigUpdateRequest(BaseModel):
    """Partial update. Unknown fields and unknown failure types are rejected."""

    model_config = ConfigDict(extra="forbid")

    # strict=True: same rules as the config file, so "10" and 1.0 are rejected
    # rather than silently coerced.
    interval_seconds: Optional[int] = Field(
        None,
        strict=True,
        ge=limits.MIN_INTERVAL_SECONDS,
        le=limits.MAX_INTERVAL_SECONDS,
    )
    dry_run: Optional[bool] = Field(None, strict=True)
    failures: Optional[Dict[FailureType, Dict[str, Any]]] = None


class FailureConfigResponse(BaseModel):
    failure_type: str
    enabled: bool
    probability: float
    config: Dict[str, Any]


# ============================================================================
# Agent Control
# ============================================================================


def run_agent_loop():
    """Background agent loop with kill switch protection."""
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
                    # Wrap injections with failure context tracking
                    if agent_state.kill_switch:
                        failure_context = agent_state.kill_switch.failure_context
                    else:
                        failure_context = None

                    if name == "cpu":
                        if failure_context:
                            wrap_injection_with_context(
                                "cpu",
                                inject_cpu,
                                failure_context,
                                cfg,
                                dry_run=config.agent.dry_run,
                            )
                        else:
                            inject_cpu(cfg, dry_run=config.agent.dry_run)

                    elif name == "memory":
                        if failure_context:
                            wrap_injection_with_context(
                                "memory",
                                inject_memory,
                                failure_context,
                                cfg,
                                dry_run=config.agent.dry_run,
                            )
                        else:
                            inject_memory(cfg, dry_run=config.agent.dry_run)

                    elif name == "process":
                        if failure_context:
                            wrap_injection_with_context(
                                "process",
                                inject_process,
                                failure_context,
                                cfg,
                                dry_run=config.agent.dry_run,
                            )
                        else:
                            inject_process(cfg, dry_run=config.agent.dry_run)

                    elif name == "network":
                        if failure_context:
                            wrap_injection_with_context(
                                "network",
                                inject_network,
                                failure_context,
                                cfg,
                                dry_run=config.agent.dry_run,
                            )
                        else:
                            inject_network(cfg, dry_run=config.agent.dry_run)

                except Exception as e:
                    logger.error(
                        f"Failure injection error: {e}",
                        exc_info=True,
                        extra={"failure_type": name},
                    )

            agent_state.stop_event.wait(config.agent.interval_seconds)

        except Exception as e:
            logger.error(f"Error in agent loop: {e}", exc_info=True)
            try:
                config = get_config()
                agent_state.stop_event.wait(config.agent.interval_seconds)
            except Exception:
                agent_state.stop_event.wait(10)

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
async def start_agent(enable_kill_switch: bool = True):
    """Start the chaos agent loop with optional kill switch."""
    if agent_state.enabled:
        raise HTTPException(status_code=400, detail="Agent already running")

    if agent_state.config is None:
        raise HTTPException(status_code=500, detail="Configuration not loaded")

    logger.info("Starting chaos agent via API")

    # Initialize kill switch if enabled
    if enable_kill_switch:
        # Get target health URL from config or use default
        # config = get_config()

        # Create kill switch with production config
        kill_switch_config = PRODUCTION_CONFIG
        # Override target URL if needed
        # kill_switch_config.target_health_url = "http://target-app:8080/health"

        agent_state.kill_switch = KillSwitch(kill_switch_config)

        # Start monitoring with callback to stop agent
        def stop_agent_callback():
            logger.critical("Kill switch triggered, stopping agent")
            agent_state.stop_event.set()

        agent_state.kill_switch.start_monitoring(stop_agent_callback)
        logger.info("Kill switch enabled and monitoring started")
    else:
        agent_state.kill_switch = None
        logger.warning(
            "Kill switch disabled - chaos will run without automatic protection"
        )

    agent_state.stop_event.clear()
    agent_state.enabled = True
    agent_state.agent_thread = threading.Thread(target=run_agent_loop, daemon=True)
    agent_state.agent_thread.start()

    return {
        "status": "started",
        "message": "Chaos agent started successfully",
        "kill_switch_enabled": enable_kill_switch,
    }


@app.post("/agent/stop", tags=["Agent Control"])
async def stop_agent():
    """Stop the chaos agent loop."""
    if not agent_state.enabled:
        raise HTTPException(status_code=400, detail="Agent not running")

    logger.info("Stopping chaos agent via API")
    agent_state.stop_event.set()

    # Stop kill switch monitoring
    if agent_state.kill_switch:
        agent_state.kill_switch.stop_monitoring()
        agent_state.kill_switch = None

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


@app.get("/kill-switch/status", tags=["Kill Switch"])
async def get_kill_switch_status():
    """Get current kill switch status."""
    if not agent_state.kill_switch:
        return {"enabled": False, "message": "Kill switch not initialized"}

    return agent_state.kill_switch.get_status()


@app.post("/kill-switch/reset", tags=["Kill Switch"])
async def reset_kill_switch():
    """Reset kill switch state (useful after manual recovery)."""
    if not agent_state.kill_switch:
        raise HTTPException(status_code=400, detail="Kill switch not initialized")

    agent_state.kill_switch.consecutive_failures = 0
    agent_state.kill_switch.consecutive_successes = 0
    agent_state.kill_switch.circuit_state = CircuitState.CLOSED

    logger.info("Kill switch state reset")

    return {"status": "reset", "message": "Kill switch state has been reset"}


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

    # Overlay any overrides on the stored config and validate the result.
    # Nothing is injected unless the merged config is within hard limits.
    try:
        failure_config = merge_failure_config(
            failure_type, config.failures[failure_type], request.config or {}
        )
    except ConfigValidationError as e:
        raise _validation_error(e)

    # Pre-flight against the live resource budget so an unsafe request fails
    # here with a clear reason instead of silently inside the background task.
    # CPU requests above budget are clamped (reported below); memory requests
    # above budget are refused.
    clamped_to: Optional[int] = None
    if failure_type == "cpu":
        grant = governor.preview_cores(failure_config["cores"])
        field = "cores"
    elif failure_type == "memory":
        grant = governor.preview_memory(failure_config["mb"])
        field = "mb"
    else:
        grant = None
        field = ""
    if grant is not None:
        if not grant.ok:
            raise HTTPException(
                status_code=422,
                detail=[{"loc": [failure_type, field], "msg": grant.reason}],
            )
        if grant.clamped:
            clamped_to = grant.granted

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

    response: Dict[str, Any] = {
        "status": "injecting",
        "failure_type": failure_type,
        "dry_run": request.dry_run,
        "message": f"Manual {failure_type} injection started",
    }
    if clamped_to is not None:
        response["clamped_cores"] = clamped_to
    return response


# ============================================================================
# Resource Budget
# ============================================================================


@app.get("/limits", tags=["Safety"])
async def get_limits():
    """
    Report the live resource budget: detected cgroup limits, how many cores and
    how much memory injections may use right now, what running injections have
    reserved, and warnings if the container has no limits.
    """
    return governor.snapshot()


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

    # Phase 1: validate everything. Nothing is mutated until all of it passes,
    # so a bad request can never leave the config half-applied.
    validated_failures: Dict[str, Dict[str, Any]] = {}
    errors: List[Dict[str, Any]] = []

    for failure_type, updates in (request.failures or {}).items():
        name = failure_type.value
        if name not in config.failures:
            raise HTTPException(
                status_code=404, detail=f"Failure type '{name}' not found"
            )
        try:
            validated_failures[name] = merge_failure_config(
                name, config.failures[name], updates
            )
        except ConfigValidationError as e:
            errors.extend(e.errors)

    if errors:
        raise _validation_error(ConfigValidationError(errors))

    # Phase 2: apply. Each failure dict is replaced whole (never edited in
    # place) so the running loop always sees a complete, valid config.
    if request.interval_seconds is not None:
        config.agent.interval_seconds = request.interval_seconds
        changes["interval_seconds"] = request.interval_seconds

    if request.dry_run is not None:
        config.agent.dry_run = request.dry_run
        changes["dry_run"] = request.dry_run

    for name, new_config in validated_failures.items():
        config.failures[name] = new_config
        changes[f"failures.{name}"] = new_config

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

    try:
        validated = merge_failure_config(
            failure_name, config.failures[failure_name], config_update
        )
    except ConfigValidationError as e:
        raise _validation_error(e)

    # Replace whole (never edit in place) so the running loop never sees a
    # partially updated config.
    config.failures[failure_name] = validated

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
        apply_settings_and_report(config.safety)
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
    except ConfigValidationError as e:
        # The previous (valid) config stays active.
        logger.error(f"Rejected invalid config on reload: {e}")
        raise _validation_error(e)
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
