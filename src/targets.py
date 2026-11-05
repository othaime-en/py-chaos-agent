"""
Target labels and selectors: which agents an experiment may run on.

The same ``config.yaml`` is often shared by many sidecars in many environments.
A *selector* on an experiment says where it is allowed to run, and each agent
knows its own *labels*. If they do not match, the experiment is refused. That
bounds the blast radius to the targets you named: an experiment written for
staging cannot run in production because someone copied the config.

Where an agent gets its labels (both are optional, the file wins on a clash):

* ``CHAOS_TARGET_LABELS``: comma-separated ``key=value`` pairs, for example
  ``env=staging,app=checkout``.
* ``CHAOS_TARGET_LABELS_FILE``: a file with one ``key="value"`` per line. That is
  exactly the format Kubernetes writes for the downward API field
  ``metadata.labels``, so a pod's own labels can be mounted as this file. The
  file is re-read about once a second, which makes relabelling the pod a remote
  off switch: remove the label an experiment selects on and a running experiment
  stops.

Label rules are Kubernetes'. A key is ``name`` or ``prefix/name``: ``name`` is up
to 63 characters of letters, digits, ``-``, ``_`` and ``.``, starting and ending
with a letter or digit, and ``prefix`` is a DNS name of up to 253 characters. A
value is empty or follows the ``name`` rule.

A selector has ``match_labels`` (every pair must be present) and
``match_expressions`` (``In``, ``NotIn``, ``Exists``, ``DoesNotExist``). All the
conditions must hold. An agent with no labels matches no selector: a missing
label never counts as a match, except for ``NotIn`` and ``DoesNotExist``, which
are satisfied by a missing key exactly as in Kubernetes.
"""

import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from .logging_config import get_logger

logger = get_logger(__name__)

LABELS_ENV = "CHAOS_TARGET_LABELS"
LABELS_FILE_ENV = "CHAOS_TARGET_LABELS_FILE"
ID_ENV = "CHAOS_TARGET_ID"

MAX_LABELS = 100
MAX_SELECTOR_ITEMS = 20
MAX_SELECTOR_VALUES = 20
_MAX_LABELS_FILE_BYTES = 64 * 1024
_CACHE_SECONDS = 1.0

_NAME_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
_PREFIX_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)
OPERATORS = ("In", "NotIn", "Exists", "DoesNotExist")


class TargetLabelsError(ValueError):
    """The labels (or a selector) are not valid."""


def validate_label_key(key: str) -> str:
    """Return ``key`` if it is a valid Kubernetes label key, else raise."""
    if not isinstance(key, str) or not key:
        raise TargetLabelsError("label key must not be empty")
    prefix, slash, name = key.rpartition("/")
    if slash:
        if len(prefix) > 253 or not _PREFIX_RE.match(prefix):
            raise TargetLabelsError(f"label key '{key}' has an invalid prefix")
    if not _NAME_RE.match(name):
        raise TargetLabelsError(
            f"label key '{key}' is invalid: the name part must be 1 to 63 "
            "letters, digits, '-', '_' or '.', starting and ending with a "
            "letter or digit"
        )
    return key


def validate_label_value(value: str, key: str = "") -> str:
    """Return ``value`` if it is a valid Kubernetes label value, else raise."""
    if not isinstance(value, str):
        raise TargetLabelsError(f"label value for '{key}' must be a string")
    if value and not _NAME_RE.match(value):
        raise TargetLabelsError(
            f"label value '{value}' for '{key}' is invalid: use up to 63 letters, "
            "digits, '-', '_' or '.', starting and ending with a letter or digit"
        )
    return value


def _check(labels: Dict[str, str]) -> Dict[str, str]:
    if len(labels) > MAX_LABELS:
        raise TargetLabelsError(f"at most {MAX_LABELS} labels are allowed")
    for key, value in labels.items():
        validate_label_key(key)
        validate_label_value(value, key)
    return labels


def parse_label_pairs(text: str) -> Dict[str, str]:
    """``"env=staging,app=checkout"`` -> ``{"env": "staging", "app": "checkout"}``."""
    labels: Dict[str, str] = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        key, equals, value = part.partition("=")
        if not equals:
            raise TargetLabelsError(f"'{part}' is not key=value")
        key = key.strip()
        if key in labels:
            raise TargetLabelsError(f"label '{key}' is given twice")
        labels[key] = value.strip()
    return _check(labels)


def parse_labels_file(text: str) -> Dict[str, str]:
    """
    Parse the downward API labels format: one ``key="value"`` per line.

    Values are quoted. A value that needs escaping cannot be a valid label value
    anyway, so anything but a plain quoted token is rejected.
    """
    labels: Dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        key, equals, raw = line.partition("=")
        if not equals:
            raise TargetLabelsError(f'line {number} is not key="value"')
        if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
            raise TargetLabelsError(
                f"line {number}: the value must be in double quotes"
            )
        key = key.strip()
        if key in labels:
            raise TargetLabelsError(f"label '{key}' is given twice")
        labels[key] = raw[1:-1]
    return _check(labels)


def validate_selector(selector: Mapping[str, Any]) -> None:
    """Raise TargetLabelsError unless ``selector`` is well formed and not empty."""
    match_labels = selector.get("match_labels") or {}
    expressions = selector.get("match_expressions") or []
    if not match_labels and not expressions:
        raise TargetLabelsError(
            "a selector needs match_labels or match_expressions; an empty "
            "selector would match every agent"
        )
    if len(match_labels) > MAX_SELECTOR_ITEMS or len(expressions) > MAX_SELECTOR_ITEMS:
        raise TargetLabelsError(f"at most {MAX_SELECTOR_ITEMS} conditions of each kind")
    for key, value in match_labels.items():
        validate_label_key(key)
        validate_label_value(value, key)
    for expr in expressions:
        validate_label_key(expr["key"])
        operator = expr["operator"]
        values = expr.get("values") or []
        if operator not in OPERATORS:
            raise TargetLabelsError(f"unknown operator '{operator}'")
        if operator in ("In", "NotIn"):
            if not values:
                raise TargetLabelsError(f"operator {operator} needs values")
            if len(values) > MAX_SELECTOR_VALUES:
                raise TargetLabelsError(f"at most {MAX_SELECTOR_VALUES} values")
            for value in values:
                validate_label_value(value, expr["key"])
        elif values:
            raise TargetLabelsError(f"operator {operator} takes no values")


@dataclass(frozen=True)
class SelectorMatch:
    """Whether labels satisfy a selector, and every condition that did not."""

    matches: bool
    problems: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"matches": self.matches, "problems": list(self.problems)}

    @property
    def summary(self) -> str:
        return "; ".join(self.problems) if self.problems else "matches"


def match(
    selector: Optional[Mapping[str, Any]], labels: Mapping[str, str]
) -> SelectorMatch:
    """
    Check ``labels`` against ``selector``. ``None`` means "no selector": it
    matches. Every unmet condition is listed so the refusal says what to fix.
    """
    if not selector:
        return SelectorMatch(True)
    problems: List[str] = []
    for key, wanted in (selector.get("match_labels") or {}).items():
        have = labels.get(key)
        if have != wanted:
            problems.append(f"{key} must be '{wanted}' but is {_shown(have)}")
    for expr in selector.get("match_expressions") or []:
        key, operator = expr["key"], expr["operator"]
        values = list(expr.get("values") or [])
        have = labels.get(key)
        listed = ", ".join(f"'{v}'" for v in values)
        if operator == "In" and have not in values:
            problems.append(f"{key} must be one of {listed} but is {_shown(have)}")
        elif operator == "NotIn" and have in values:
            problems.append(f"{key} must not be one of {listed} but is '{have}'")
        elif operator == "Exists" and key not in labels:
            problems.append(f"{key} must be set but is not")
        elif operator == "DoesNotExist" and key in labels:
            problems.append(f"{key} must not be set but is '{labels[key]}'")
    return SelectorMatch(not problems, problems)


def _shown(value: Optional[str]) -> str:
    return "not set" if value is None else f"'{value}'"


class TargetLabels:
    """
    This agent's labels, read from the environment and an optional file.

    ``get()`` is cheap (cached for about a second) so a running experiment can
    check it often. It fails closed: if the file is configured but cannot be read
    or is invalid, the labels are empty, so selectors stop matching and a running
    experiment is aborted, rather than carrying on with stale labels.
    """

    def __init__(
        self,
        environ: Optional[Mapping[str, str]] = None,
        cache_seconds: float = _CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._environ = environ
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._labels: Dict[str, str] = {}
        self._loaded_at: Optional[float] = None
        self._error: Optional[str] = None

    def _env(self) -> Mapping[str, str]:
        return self._environ if self._environ is not None else os.environ

    def load(self) -> Dict[str, str]:
        """Read the labels now, raising TargetLabelsError if they are invalid."""
        env = self._env()
        labels = parse_label_pairs(env.get(LABELS_ENV, ""))
        path = env.get(LABELS_FILE_ENV, "").strip()
        if path:
            try:
                with open(path, "rb") as f:
                    raw = f.read(_MAX_LABELS_FILE_BYTES + 1)
            except OSError as e:
                raise TargetLabelsError(
                    f"cannot read {LABELS_FILE_ENV} ({type(e).__name__})"
                ) from None
            if len(raw) > _MAX_LABELS_FILE_BYTES:
                raise TargetLabelsError(f"{LABELS_FILE_ENV} is too large")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise TargetLabelsError(f"{LABELS_FILE_ENV} is not UTF-8") from None
            labels.update(parse_labels_file(text))
        return _check(labels)

    def validate_startup(self) -> Dict[str, str]:
        """Load once and raise TargetLabelsError if anything is wrong."""
        labels = self.load()
        with self._lock:
            self._labels, self._loaded_at, self._error = labels, self._clock(), None
        return dict(labels)

    def get(self) -> Dict[str, str]:
        """The current labels (a copy). Empty if they cannot be read."""
        with self._lock:
            now = self._clock()
            fresh = (
                self._loaded_at is not None
                and now - self._loaded_at < self._cache_seconds
            )
            if not fresh:
                try:
                    self._labels = self.load()
                    if self._error:
                        logger.info("Target labels readable again")
                    self._error = None
                except TargetLabelsError as e:
                    self._labels = {}
                    if str(e) != self._error:
                        logger.error(
                            "Target labels unreadable, treating the agent as "
                            "having none",
                            extra={"error": str(e)},
                        )
                    self._error = str(e)
                self._loaded_at = now
            return dict(self._labels)

    @property
    def error(self) -> Optional[str]:
        """Why the labels could not be read the last time, or ``None``."""
        with self._lock:
            return self._error

    def reset(self) -> None:
        with self._lock:
            self._labels, self._loaded_at, self._error = {}, None, None


def agent_id(environ: Optional[Mapping[str, str]] = None) -> str:
    """
    A name for this agent that is stable across restarts of the same pod: the
    ``CHAOS_TARGET_ID`` setting, or else the host name (the pod name on
    Kubernetes). Used to decide, deterministically, whether this agent is in an
    experiment's sample.
    """
    env = environ if environ is not None else os.environ
    configured = env.get(ID_ENV, "").strip()
    return configured or socket.gethostname()


# The agent's labels. Read lazily from the environment, so it also works in
# processes that were not started through api_server (uvicorn --reload).
agent_labels = TargetLabels()
