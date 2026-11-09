"""
FastAPI interface for chaos control.

Provides programmatic control over chaos injections through REST API.
"""

from fastapi import Depends, FastAPI, HTTPException, BackgroundTasks, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, Dict, Any, List
from enum import Enum
import os
import threading
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager, nullcontext

from .audit import AuditError, AuditMiddleware, annotate, audit
from .auth import auth_disabled, get_expected_token, require_auth
from . import limits
from .config import Config, load_config, validate_config
from .engine import ChaosEngine, Settings, run_injection, snapshot_settings
from . import selection as selecting
from . import targets
from . import windows as time_windows
from .experiments import (
    ExperimentBusy,
    ExperimentRunner,
    OutsideWindow,
    TargetMismatch,
)
from .failures.network import reconcile_stale_rules
from .lifecycle import lifecycle
from .resources import apply_settings_and_report, governor
from .schemas import (
    ConfigValidationError,
    merge_failure_config,
)
from .logging_config import get_logger
from .metrics import (
    INJECTIONS_TOTAL,
    INJECTION_ACTIVE,
    metric_value,
    reset_metrics as clear_injection_metrics,
)
from .kill_switch import KillSwitch, KillSwitchOpen

logger = get_logger(__name__)


# Global state with proper typing
class AgentState:
    """Global state for the chaos agent."""

    enabled: bool = False
    config: Optional[Config] = None
    # Guards ``config`` against the engine thread. Every write to the config
    # (PATCH, reload) takes it, and so does the engine's per-tick snapshot, so
    # a tick sees either the old config or the new one, never a mix.
    config_lock: threading.RLock = threading.RLock()
    # The loop. Created below, once the callbacks it needs are defined.
    engine: ChaosEngine
    kill_switch: Optional[KillSwitch] = None
    # True only if the loop was started with the kill switch explicitly turned
    # off (and the config allows that). Defaults to protected.
    kill_switch_bypass: bool = False
    # Runs named experiments. Created below, once the kill switch lookup exists.
    experiments: ExperimentRunner


agent_state = AgentState()


def get_config() -> Config:
    """Get config with type safety."""
    if agent_state.config is None:
        raise HTTPException(status_code=500, detail="Configuration not loaded")
    return agent_state.config


def _chaos_armed() -> bool:
    """Chaos is active while the loop or an experiment runs, or any injection is
    in flight."""
    return (
        agent_state.enabled
        or agent_state.experiments.is_running
        or bool(lifecycle.active_types())
    )


def _on_kill_switch_trip() -> None:
    """Called when the kill switch opens: do not let a running injection finish
    its full duration against an unhealthy target."""
    lifecycle.abort_active("kill switch triggered")


def apply_kill_switch_config(config: Config) -> None:
    """
    Create or update the process-wide kill switch from the config.

    One kill switch lives for the life of the process, so stopping and starting
    the agent (or reloading the config) can never reset a tripped breaker.
    """
    ks = agent_state.kill_switch
    if ks is not None:
        ks.update_config(config.kill_switch)  # keeps breaker state
        if config.kill_switch.enabled:
            ks.start_monitoring(_on_kill_switch_trip)
        return

    if not config.kill_switch.enabled:
        logger.warning(
            "Kill switch is disabled in config: chaos will run without "
            "automatic protection"
        )
        return

    ks = KillSwitch(config.kill_switch, is_armed=_chaos_armed)
    ks.start_monitoring(_on_kill_switch_trip)
    agent_state.kill_switch = ks


def _active_kill_switch() -> Optional[KillSwitch]:
    """The kill switch that should gate injections right now, if any."""
    if agent_state.kill_switch_bypass:
        return None
    ks = agent_state.kill_switch
    return ks if ks is not None and ks.config.enabled else None


def _eligibility_rule() -> Optional[Dict[str, Any]]:
    """The agent-wide opt-in rule from the config (``agent.eligibility``), or None."""
    config = agent_state.config
    return config.agent.eligibility if config is not None else None


def _eligibility_reason() -> Optional[str]:
    """Why this agent may not run chaos right now, or None if it may."""
    rule = _eligibility_rule()
    if not rule:
        return None
    return selecting.eligibility_problem(rule, targets.agent_labels.get())


def _settings_snapshot() -> Optional[Settings]:
    """A private copy of the loop settings, taken under the config lock."""
    with agent_state.config_lock:
        config = agent_state.config
        return snapshot_settings(config) if config is not None else None


def _on_engine_exit() -> None:
    agent_state.enabled = False


agent_state.engine = ChaosEngine(
    settings_provider=_settings_snapshot,
    kill_switch_provider=_active_kill_switch,
    eligibility_provider=_eligibility_reason,
    on_exit=_on_engine_exit,
    name="chaos-api",
)


# Experiments are gated by the same kill switch as everything else. It is looked
# up on every use, so a kill switch created or swapped later is honored.
agent_state.experiments = ExperimentRunner(
    kill_switch_provider=_active_kill_switch,
    eligibility_provider=_eligibility_rule,
)


def _begin_agent_loop() -> None:
    """Start the agent loop. Used by POST /agent/start and auto_start."""
    agent_state.enabled = True
    agent_state.engine.start()


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
        # A requested audit file that cannot be written stops startup: running
        # without the record the operator asked for is worse than not running.
        audit.configure_from_env()
    except AuditError as e:
        logger.critical("Audit log unavailable", extra={"error": str(e)})
        raise
    try:
        config = load_config()
        agent_state.config = config
        logger.info("API started, configuration loaded")
        for warning in validate_config(config):
            logger.warning("Configuration warning", extra={"warning": warning})
        apply_settings_and_report(config.safety)
        apply_kill_switch_config(config)

        # Repair a netem rule left by a previous run that was SIGKILLed.
        if "network" in config.failures:
            reconcile_stale_rules([config.failures["network"].get("interface", "eth0")])

        # Opt-in: begin injecting without waiting for POST /agent/start. The kill
        # switch and safety settings above are already in place.
        if config.agent.auto_start:
            logger.info("Starting chaos agent automatically (agent.auto_start)")
            _begin_agent_loop()
    except Exception as e:
        logger.error(f"Failed to load config on startup: {e}")
        agent_state.config = None

    # Backstop for exits that bypass signal handling.
    lifecycle.install_atexit()

    # Surface auth misconfiguration immediately instead of on first request.
    try:
        if not auth_disabled() and get_expected_token() is None:
            logger.error(
                "No API token configured: all protected endpoints will return 503"
            )
    except ValueError as e:
        logger.error(f"Invalid API token configuration: {e}")

    yield

    # Shutdown: stop the loop, abort in-flight injections, remove their effects.
    # (src.api_server also does this from the signal handler, because uvicorn
    # waits for running background tasks before it reaches this point.)
    if agent_state.enabled:
        logger.info("Shutting down agent on API shutdown")
    lifecycle.shutdown("api shutdown")
    agent_state.experiments.stop(timeout=10, reason="agent shutting down")
    agent_state.engine.stop(timeout=5, reason="api shutdown")
    if agent_state.kill_switch is not None:
        agent_state.kill_switch.stop_monitoring()
        agent_state.kill_switch = None


# Interactive docs expose the full API surface and are served outside the
# app-level auth dependency, so they are off unless explicitly enabled.
_docs_enabled = os.environ.get("CHAOS_API_ENABLE_DOCS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

app = FastAPI(
    title="chaos API",
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


app.add_middleware(AuditMiddleware)


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
    active_injections: List[str] = []
    paused_by_kill_switch: bool = False
    active_experiment: Optional[str] = None
    # False while the agent-wide opt-in rule (agent.eligibility) is not met.
    eligible: bool = True
    eligibility_reason: Optional[str] = None


class ExperimentRunRequest(BaseModel):
    """
    ``dry_run`` overrides the experiment's own setting when given.
    ``sample_key`` picks which agents fall in a ``sample_percent`` sample: the same
    key picks the same agents, a new key (a date, say) draws a new sample.
    """

    model_config = ConfigDict(extra="forbid")

    dry_run: Optional[bool] = Field(None, strict=True)
    sample_key: str = Field("", pattern=r"^[A-Za-z0-9._:-]{0,64}$", strict=True)


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


# ============================================================================
# API Endpoints
# ============================================================================


@app.get("/", tags=["General"])
async def root():
    """API root - health check."""
    return {
        "service": "chaos",
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
    started = agent_state.engine.start_time
    if agent_state.enabled and started is not None:
        uptime = time.time() - started

    enabled_failures = [name for name, cfg in config.failures.items() if cfg["enabled"]]
    ks = _active_kill_switch()
    current = agent_state.experiments.current()
    eligibility_reason = _eligibility_reason()

    return AgentStatus(
        enabled=agent_state.enabled,
        uptime_seconds=uptime,
        config_loaded=True,
        dry_run=config.agent.dry_run,
        interval_seconds=config.agent.interval_seconds,
        enabled_failures=enabled_failures,
        active_injections=lifecycle.active_types(),
        paused_by_kill_switch=bool(ks and ks.paused),
        active_experiment=(current or {}).get("experiment"),
        eligible=eligibility_reason is None,
        eligibility_reason=eligibility_reason,
    )


@app.post("/agent/start", tags=["Agent Control"])
async def start_agent(http_request: Request, enable_kill_switch: Optional[bool] = None):
    """
    Start the chaos agent loop.

    The kill switch is controlled by the ``kill_switch`` section of the config,
    not by this request. ``enable_kill_switch=false`` is honored only if the
    config sets ``kill_switch.allow_api_disable: true``; otherwise it is refused.
    """
    annotate(http_request, enable_kill_switch=enable_kill_switch)
    if agent_state.enabled:
        raise HTTPException(status_code=400, detail="Agent already running")

    if agent_state.config is None:
        raise HTTPException(status_code=500, detail="Configuration not loaded")

    if agent_state.experiments.is_running:
        raise HTTPException(
            status_code=409,
            detail=(
                "An experiment is running; the agent loop cannot start until it "
                "ends (POST /experiment-runs/abort to end it)"
            ),
        )

    config = agent_state.config
    logger.info("Starting chaos agent via API")

    if enable_kill_switch is False:
        if config.kill_switch.enabled and not config.kill_switch.allow_api_disable:
            raise HTTPException(
                status_code=403,
                detail=(
                    "The kill switch cannot be disabled through the API "
                    "(kill_switch.allow_api_disable is false)"
                ),
            )
        agent_state.kill_switch_bypass = True
        logger.warning(
            "Kill switch bypassed for this run: chaos will run without "
            "automatic protection"
        )
    else:
        agent_state.kill_switch_bypass = False

    # Normally created at startup. Create it now if it is missing (for example
    # if the config was fixed after a failed first load).
    if agent_state.kill_switch is None:
        apply_kill_switch_config(config)

    _begin_agent_loop()

    ks = _active_kill_switch()
    return {
        "status": "started",
        "message": "Chaos agent started successfully",
        "kill_switch_enabled": ks is not None,
        "kill_switch_state": ks.circuit_state.value if ks else None,
        "paused_by_kill_switch": bool(ks and ks.paused),
    }


@app.post("/agent/stop", tags=["Agent Control"])
async def stop_agent():
    """Stop the chaos agent loop."""
    if not agent_state.enabled:
        raise HTTPException(status_code=400, detail="Agent not running")

    logger.info("Stopping chaos agent via API")
    # Ends the loop and cuts short any running injection, so its effect is
    # removed now, not when its duration would have ended. Waits briefly for
    # the loop and its injections to finish.
    agent_state.engine.stop(timeout=5, reason="agent stopped")

    # The kill switch keeps running. It lives for the whole process, so stopping
    # and starting the agent cannot be used to clear a tripped breaker.

    agent_state.enabled = False

    return {"status": "stopped", "message": "Chaos agent stopped successfully"}


@app.post("/agent/restart", tags=["Agent Control"])
async def restart_agent(http_request: Request):
    """Restart the chaos agent loop."""
    if agent_state.enabled:
        await stop_agent()
        time.sleep(1)

    return await start_agent(http_request)


@app.get("/kill-switch/status", tags=["Kill Switch"])
async def get_kill_switch_status():
    """Current kill switch state, last health check, and trip history."""
    ks = agent_state.kill_switch
    if ks is None:
        return {"enabled": False, "message": "Kill switch is not enabled"}

    status = ks.get_status()
    status["bypassed"] = agent_state.kill_switch_bypass
    return status


@app.post("/kill-switch/reset", tags=["Kill Switch"])
async def reset_kill_switch():
    """
    Close the kill switch and clear any latch.

    The target must pass a health check right now, so a reset cannot be used to
    resume chaos against a target that is still down. Returns 409 if it does not.
    """
    ks = agent_state.kill_switch
    if ks is None:
        raise HTTPException(status_code=400, detail="Kill switch is not enabled")

    ok, message = ks.reset()
    if not ok:
        raise HTTPException(status_code=409, detail=message)

    logger.info("Kill switch reset via API")
    return {"status": "reset", "message": message, **ks.get_status()}


# ============================================================================
# Manual Injections
# ============================================================================


@app.post("/inject/manual", tags=["Injections"])
async def manual_injection(
    request: ManualInjectionRequest,
    background_tasks: BackgroundTasks,
    http_request: Request,
):
    """
    Manually trigger a single failure injection.

    This bypasses probability checks and injects immediately.
    """
    config = get_config()
    failure_type = request.failure_type.value
    annotate(http_request, failure_type=failure_type, dry_run=request.dry_run)

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

    if not request.dry_run and lifecycle.is_shutting_down():
        raise HTTPException(status_code=503, detail="Agent is shutting down")

    # The kill switch gates manual injections too: never inject into a target
    # that has been found unhealthy. Dry runs change nothing and are allowed.
    # This comes before the "already running" check on purpose: when the breaker
    # trips, the aborted injection removes its effect first and releases its
    # guard a moment later. In that gap the honest answer is "paused", not
    # "already running".
    ks = _active_kill_switch()
    if ks is not None and not request.dry_run:
        allowed, reason = ks.allows_injection()
        if not allowed:
            raise HTTPException(status_code=409, detail=f"Chaos paused: {reason}")

    if not request.dry_run:
        ineligible = _eligibility_reason()
        if ineligible:
            raise HTTPException(
                status_code=409,
                detail=f"This agent is not eligible for chaos: {ineligible}",
            )

    if not request.dry_run and agent_state.experiments.is_running:
        raise HTTPException(
            status_code=409,
            detail=(
                "An experiment is running; manual injections would muddy its "
                "result (POST /experiment-runs/abort to end it)"
            ),
        )

    if not request.dry_run and lifecycle.is_active(failure_type):
        raise HTTPException(
            status_code=409,
            detail=f"A {failure_type} injection is already running",
        )

    logger.info(
        f"Manual injection requested: {failure_type}",
        extra={"failure_type": failure_type, "dry_run": request.dry_run},
    )

    who = getattr(http_request.state, "principal", None)
    audit_who: Dict[str, Any] = {
        "principal": who.name if who else "unknown",
        "role": who.role if who else None,
    }

    def inject():
        audit.record(
            "injection.started",
            failure_type=failure_type,
            dry_run=request.dry_run,
            source="manual",
            **audit_who,
        )
        try:
            # Re-checked here (not just above) in case the breaker tripped
            # between the request and the background task starting. Also records
            # the injection so a process kill earns its grace period.
            with ks.injection(failure_type, request.dry_run) if ks else nullcontext():
                run_injection(failure_type, failure_config, request.dry_run)
            audit.record(
                "injection.completed",
                failure_type=failure_type,
                source="manual",
                **audit_who,
            )
        except KillSwitchOpen as e:
            logger.warning(
                "Manual injection skipped: kill switch tripped before it started",
                extra={"failure_type": failure_type, "reason": e.reason},
            )
            audit.record(
                "injection.refused",
                failure_type=failure_type,
                source="manual",
                reason=e.reason,
                **audit_who,
            )
        except Exception as e:
            logger.error(f"Manual injection failed: {e}", exc_info=True)
            audit.record(
                "injection.failed",
                failure_type=failure_type,
                source="manual",
                error=str(e),
                **audit_who,
            )

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


@app.post("/inject/abort", tags=["Injections"])
async def abort_injections():
    """
    Cut short every injection that is running right now (manual or from the
    agent loop). Their effects are removed immediately instead of at the end of
    their duration. Does not stop the agent loop; use /agent/stop for that.
    """
    aborted = lifecycle.abort_active("aborted via API")
    return {"status": "aborted", "aborted": aborted}


# ============================================================================
# Experiments
# ============================================================================


def _window_state(exp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Whether the experiment's time window is open right now, or ``None`` when
    it has no windows (always allowed)."""
    spec = exp.get("windows")
    if not spec:
        return None
    status = time_windows.evaluate(spec, time_windows.utcnow())
    return {"timezone": spec["timezone"], **status.to_dict()}


def _target_state(exp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Whether this agent is a target of the experiment right now, or ``None``
    when it has no selector (it runs anywhere)."""
    selector = exp.get("selector")
    if not selector:
        return None
    return targets.match(selector, agent_state.experiments.target_labels()).to_dict()


def _experiment_summary(exp: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": exp["name"],
        "description": exp["description"],
        "hypothesis": exp["hypothesis"],
        "faults": sorted(exp["faults"]),
        "duration_seconds": exp["schedule"]["duration_seconds"],
        "dry_run": exp["dry_run"],
        "window": _window_state(exp),
        "target": _target_state(exp),
    }


def _find_experiment(name: str) -> Dict[str, Any]:
    config = get_config()
    exp = config.experiments.get(name)
    if exp is None:
        raise HTTPException(status_code=404, detail=f"Experiment '{name}' not found")
    return exp


@app.get("/target", tags=["Experiments"])
async def get_target():
    """
    The labels this agent has, which experiment selectors are matched against
    (``CHAOS_TARGET_LABELS`` and ``CHAOS_TARGET_LABELS_FILE``). ``labels_error`` says
    why they could not be read; in that case the labels are empty and selectors
    do not match.
    """
    return {
        "agent_id": agent_state.experiments.agent_id(),
        "labels": agent_state.experiments.target_labels(),
        "labels_error": targets.agent_labels.error,
    }


@app.get("/experiments", tags=["Experiments"])
async def list_experiments():
    """The experiments defined in the config file."""
    config = get_config()
    experiments = [_experiment_summary(e) for e in config.experiments.values()]
    return {"count": len(experiments), "experiments": experiments}


@app.get("/experiments/{name}", tags=["Experiments"])
async def get_experiment(name: str):
    """
    One experiment in full: hypothesis, steady state, faults, schedule, selector
    and time windows, plus whether its window is open right now
    (``window_status``) and whether this agent matches its selector
    (``target_status``).
    """
    exp = _find_experiment(name)
    return {
        **exp,
        "window_status": _window_state(exp),
        "target_status": _target_state(exp),
    }


def _busy_reason() -> Optional[str]:
    if agent_state.enabled:
        return "the agent loop is running"
    current = agent_state.experiments.current()
    if current:
        return f"experiment '{current['experiment']}' is running"
    active = lifecycle.active_types()
    if active:
        return "an injection is running (" + ", ".join(active) + ")"
    return None


@app.get("/experiments/{name}/selection", tags=["Experiments"])
async def preview_selection(
    name: str,
    sample_key: str = Query("", pattern=r"^[A-Za-z0-9._:-]{0,64}$"),
):
    """
    Would this agent run the experiment right now, and if not, why not?

    Checks every gate in order (eligibility, selector, window, sample, kill switch,
    anything already running), reports each with its reason, and changes nothing.
    ``would_run`` is true only if all pass. The resource budget is checked when you
    actually start a run, not here.
    """
    exp = _find_experiment(name)
    ks = _active_kill_switch()
    ks_reason = None
    if ks is not None:
        allowed, why = ks.allows_injection()
        ks_reason = None if allowed else why
    result = agent_state.experiments.select(
        exp,
        sample_key,
        kill_switch_reason=ks_reason,
        busy_reason=_busy_reason(),
        report_state=True,
    )
    return {
        "experiment": name,
        "agent_id": agent_state.experiments.agent_id(),
        "sample_key": sample_key,
        "would_run": result.eligible,
        **result.to_dict(),
    }


@app.post("/experiments/{name}/run", status_code=202, tags=["Experiments"])
async def run_experiment(
    name: str,
    http_request: Request,
    request: Optional[ExperimentRunRequest] = None,
):
    """
    Start an experiment. Returns at once with the new run; poll
    ``GET /experiment-runs/{id}`` for the outcome.

    Refused with 409 while the agent loop, another experiment, or a manual
    injection is running, while the kill switch is holding chaos back, when this
    agent is not eligible, is not selected by the experiment's selector or its
    sample, or is outside the experiment's allowed time windows or inside a
    blackout (or when its chaos would not finish before the window closes). A dry
    run is not refused for any of those: it runs and records what a real run would
    have been told (see GET /experiments/{name}/selection to ask without running).
    Faults that do not fit the resource budget are refused with 422, like a manual
    injection.
    """
    exp = _find_experiment(name)
    dry_run = request.dry_run if request else None
    sample_key = request.sample_key if request else ""
    effective_dry_run = exp["dry_run"] if dry_run is None else dry_run
    annotate(http_request, experiment=name, dry_run=effective_dry_run)

    if not effective_dry_run and lifecycle.is_shutting_down():
        raise HTTPException(status_code=503, detail="Agent is shutting down")

    if agent_state.enabled:
        raise HTTPException(
            status_code=409,
            detail="The agent loop is running; stop it before running an experiment",
        )
    if not effective_dry_run and lifecycle.active_types():
        raise HTTPException(
            status_code=409,
            detail=(
                "An injection is running ("
                + ", ".join(lifecycle.active_types())
                + "); wait for it to end or abort it first"
            ),
        )

    ks = _active_kill_switch()
    if ks is not None and not effective_dry_run:
        allowed, reason = ks.allows_injection()
        if not allowed:
            raise HTTPException(status_code=409, detail=f"Chaos paused: {reason}")

    # Same pre-flight as a manual injection: an unsafe fault fails here with a
    # clear reason instead of silently inside the run.
    problems: List[Dict[str, Any]] = []
    clamped: Dict[str, int] = {}
    for fault, cfg in exp["faults"].items():
        if not cfg["enabled"]:
            continue
        if fault == "cpu":
            grant, field = governor.preview_cores(cfg["cores"]), "cores"
        elif fault == "memory":
            grant, field = governor.preview_memory(cfg["mb"]), "mb"
        else:
            continue
        if not grant.ok:
            problems.append(
                {
                    "loc": ["experiments", name, "faults", fault, field],
                    "msg": grant.reason,
                }
            )
        elif grant.clamped:
            clamped[f"{fault}.{field}"] = grant.granted
    if problems and not effective_dry_run:
        raise HTTPException(status_code=422, detail=problems)

    who = getattr(http_request.state, "principal", None)
    try:
        run = agent_state.experiments.start(
            exp,
            principal=who.name if who else "unknown",
            role=who.role if who else None,
            dry_run=dry_run,
            sample_key=sample_key,
        )
    except (ExperimentBusy, OutsideWindow, TargetMismatch) as e:
        raise HTTPException(status_code=409, detail=str(e))

    logger.info(
        "Experiment requested via API",
        extra={"experiment": name, "run_id": run["id"], "dry_run": effective_dry_run},
    )
    response: Dict[str, Any] = {
        "status": "started",
        "message": f"Experiment '{name}' started",
        "run": run,
    }
    if clamped:
        response["clamped"] = clamped
    return response


@app.get("/experiment-runs", tags=["Experiments"])
async def list_experiment_runs(limit: int = Query(20, ge=1, le=50)):
    """Recent experiment runs, newest first (the last 50 are remembered)."""
    runs = agent_state.experiments.history(limit=limit)
    return {"count": len(runs), "runs": runs}


@app.post("/experiment-runs/abort", tags=["Experiments"])
async def abort_experiment(http_request: Request):
    """
    End the running experiment now. Its injections are cut short and their
    effects removed. The run finishes as ``aborted`` with an inconclusive verdict.
    """
    who = getattr(http_request.state, "principal", None)
    name = who.name if who else "unknown"
    current = agent_state.experiments.current()
    if current is None or not agent_state.experiments.abort(f"aborted by {name}"):
        raise HTTPException(status_code=400, detail="No experiment is running")
    annotate(http_request, experiment=current["experiment"], run_id=current["id"])
    return {"status": "aborting", "run": current}


@app.get("/experiment-runs/{run_id}", tags=["Experiments"])
async def get_experiment_run(run_id: str):
    """One run: its status, verdict, and the steady-state results around the chaos."""
    run = agent_state.experiments.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


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
async def update_config(request: ConfigUpdateRequest, http_request: Request):
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

    # Phase 2: apply, all under the config lock. The loop snapshots the config
    # under the same lock, so it sees this request entirely or not at all.
    # Each failure dict is replaced whole (never edited in place).
    with agent_state.config_lock:
        if request.interval_seconds is not None:
            config.agent.interval_seconds = request.interval_seconds
            changes["interval_seconds"] = request.interval_seconds

        if request.dry_run is not None:
            config.agent.dry_run = request.dry_run
            changes["dry_run"] = request.dry_run

        for name, new_config in validated_failures.items():
            config.failures[name] = new_config
            changes[f"failures.{name}"] = new_config

    annotate(http_request, changes=changes)
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
    failure_type: FailureType, config_update: Dict[str, Any], http_request: Request
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

    # Replace whole (never edit in place), under the lock the loop snapshots
    # with, so it never sees a partially updated config.
    with agent_state.config_lock:
        config.failures[failure_name] = validated

    annotate(http_request, failure_type=failure_name, updates=config_update)
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
        with agent_state.config_lock:
            previous = agent_state.config
            agent_state.config = config
        apply_settings_and_report(config.safety)
        apply_kill_switch_config(config)
        logger.info("Configuration reloaded from file")

        # The running loop snapshots the config every iteration, so failure
        # settings, interval and dry_run apply on the next tick, and the kill
        # switch and safety settings were applied just above. Only logging is
        # fixed at startup.
        note = None
        if (
            previous is not None
            and previous.get_logging_config() != config.get_logging_config()
        ):
            note = (
                "Logging settings are read at startup; restart to apply them. "
                "Everything else is already in effect."
            )

        return {
            "status": "reloaded",
            "message": "Configuration reloaded successfully from config.yaml",
            "note": note,
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


@app.get("/audit", tags=["Audit"])
async def read_audit_log(
    limit: int = Query(100, ge=1, le=500),
    principal: Optional[str] = None,
    event: Optional[str] = None,
):
    """
    The latest audit events, newest first (admin only).

    This is the in-memory view of the last 500 events. The complete record is the
    audit log file (CHAOS_AUDIT_LOG_FILE) and the ``chaos.audit`` log stream.
    """
    events = audit.recent(limit=limit, principal=principal, event=event)
    return {"count": len(events), "events": events}


@app.get("/metrics/summary", tags=["Metrics"])
async def get_metrics_summary():
    """Get summary of chaos injection metrics."""

    # Collect metrics for all failure types
    summary = {}

    for failure_type in ["cpu", "memory", "process", "network"]:
        counts = {
            status: metric_value(
                INJECTIONS_TOTAL, failure_type=failure_type, status=status
            )
            for status in ("success", "aborted", "failed", "skipped")
        }
        active = metric_value(INJECTION_ACTIVE, failure_type=failure_type)

        summary[failure_type] = {
            **counts,
            "active": active,
            "total": sum(counts.values()),
        }

    return {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "metrics": summary,
    }


@app.post("/metrics/reset", tags=["Metrics"])
async def reset_metrics():
    """Reset all metrics counters (useful for testing)."""
    logger.warning("Metrics reset requested via API")

    clear_injection_metrics()

    return {
        "status": "reset",
        "message": "All metrics have been reset",
    }
