"""Log format selection from environment variables and config."""

import logging

import pytest

from src import logging_config
from src.logging_config import StructuredFormatter, setup_logging


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch):
    """setup_logging replaces the root logger's handlers, so put them back."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    for var in (
        "LOG_FORMAT",
        "JSON_LOGS",
        "LOG_LEVEL",
        "ENABLE_FILE_LOGGING",
        "ENABLE_CONSOLE_LOGGING",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ENABLE_FILE_LOGGING", "false")
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def is_json(config=None):
    setup_logging(config)
    (handler,) = [
        h for h in logging.getLogger().handlers if type(h) is logging.StreamHandler
    ]
    assert isinstance(handler.formatter, StructuredFormatter)
    return handler.formatter.json_format


class TestFormatSelection:
    def test_default_is_text(self):
        assert is_json() is False

    def test_config_file_can_select_json(self):
        assert is_json({"format": "json"}) is True

    def test_log_format_env_selects_json(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "json")
        assert is_json() is True

    def test_log_format_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "JSON")
        assert is_json() is True

    def test_log_format_env_overrides_the_config_file(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "text")
        assert is_json({"format": "json"}) is False


class TestLegacyJsonLogsVariable:
    """The deployment files used to set JSON_LOGS, which nothing read."""

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes"])
    def test_true_values_select_json(self, monkeypatch, value):
        monkeypatch.setenv("JSON_LOGS", value)
        assert is_json() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "", "maybe"])
    def test_other_values_do_not_force_json(self, monkeypatch, value):
        monkeypatch.setenv("JSON_LOGS", value)
        assert is_json() is False

    def test_false_does_not_override_a_json_config(self, monkeypatch):
        monkeypatch.setenv("JSON_LOGS", "false")
        assert is_json({"format": "json"}) is True

    def test_log_format_wins_over_json_logs(self, monkeypatch):
        monkeypatch.setenv("JSON_LOGS", "true")
        monkeypatch.setenv("LOG_FORMAT", "text")
        assert is_json() is False

    def test_helper_reports_json_only_for_true_values(self, monkeypatch):
        monkeypatch.delenv("JSON_LOGS", raising=False)
        assert logging_config._legacy_json_logs() is None
        monkeypatch.setenv("JSON_LOGS", "true")
        assert logging_config._legacy_json_logs() == "json"


class TestTimestamps:
    def test_json_timestamp_is_utc_with_a_z_suffix(self):
        import json

        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        out = json.loads(StructuredFormatter(json_format=True).format(record))
        assert out["timestamp"].endswith("Z")
        assert "+00:00" not in out["timestamp"]

    def test_no_deprecation_warning_when_formatting(self):
        import warnings

        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            StructuredFormatter(json_format=True).format(record)

    def test_timestamp_parses_back_to_a_recent_utc_time(self):
        import json
        from datetime import datetime, timedelta, timezone

        record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", None, None)
        stamp = json.loads(StructuredFormatter(json_format=True).format(record))[
            "timestamp"
        ]
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        assert abs(datetime.now(timezone.utc) - parsed) < timedelta(seconds=5)
