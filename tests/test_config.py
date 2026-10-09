import pytest
from src.config import load_config


class TestConfigLoading:
    """Test configuration file loading."""

    def test_load_config_valid(self, tmp_path):
        """Test loading a valid config file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
agent:
  interval_seconds: 10
  dry_run: false
failures:
  cpu:
    enabled: true
    probability: 0.5
    duration_seconds: 5
    cores: 2
        """)

        config = load_config(str(config_file))
        assert config.agent.interval_seconds == 10
        assert config.agent.dry_run is False
        assert config.failures["cpu"]["enabled"] is True
        assert config.failures["cpu"]["cores"] == 2
        assert config.failures["cpu"]["probability"] == 0.5

    def test_load_config_with_dry_run(self, tmp_path):
        """Test config with dry_run enabled."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
agent:
  interval_seconds: 5
  dry_run: true
failures:
  cpu:
    enabled: true
    probability: 0.3
    duration_seconds: 2
    cores: 1
        """)

        config = load_config(str(config_file))
        assert config.agent.dry_run is True

    def test_load_config_missing_file(self):
        """Test handling of missing config file."""
        with pytest.raises(FileNotFoundError):
            load_config("nonexistent.yaml")

    def test_load_config_all_failures(self, tmp_path):
        """Test loading config with all failure types."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
agent:
  interval_seconds: 15
  dry_run: false
failures:
  cpu:
    enabled: true
    probability: 0.4
    duration_seconds: 5
    cores: 2
  memory:
    enabled: true
    probability: 0.3
    duration_seconds: 8
    mb: 200
  process:
    enabled: true
    probability: 0.5
    target_name: "test-app"
  network:
    enabled: true
    probability: 0.25
    interface: "eth0"
    delay_ms: 300
    duration_seconds: 10
        """)

        config = load_config(str(config_file))
        assert len(config.failures) == 4
        assert "cpu" in config.failures
        assert "memory" in config.failures
        assert "process" in config.failures
        assert "network" in config.failures

    def test_load_config_disabled_failures(self, tmp_path):
        """Test config with disabled failure types."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
agent:
  interval_seconds: 10
  dry_run: false
failures:
  cpu:
    enabled: false
    probability: 0.5
    duration_seconds: 5
    cores: 2
        """)

        config = load_config(str(config_file))
        assert config.failures["cpu"]["enabled"] is False


class TestConfigValidation:
    """load_config rejects bad values instead of failing mid-injection."""

    def _load(self, tmp_path, body):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(body)
        return load_config(str(config_file))

    def test_out_of_range_value_rejected(self, tmp_path):
        from src.schemas import ConfigValidationError

        with pytest.raises(ConfigValidationError, match="failures.cpu.cores"):
            self._load(
                tmp_path,
                """
agent: {interval_seconds: 10}
failures:
  cpu: {enabled: true, probability: 0.5, duration_seconds: 5, cores: 100000}
""",
            )

    def test_string_number_rejected(self, tmp_path):
        from src.schemas import ConfigValidationError

        with pytest.raises(ConfigValidationError, match="interval_seconds"):
            self._load(
                tmp_path,
                """
agent: {interval_seconds: "abc"}
failures: {}
""",
            )

    def test_typo_key_rejected(self, tmp_path):
        from src.schemas import ConfigValidationError

        with pytest.raises(ConfigValidationError, match="duraton_seconds"):
            self._load(
                tmp_path,
                """
agent: {}
failures:
  cpu: {duration_seconds: 5, duraton_seconds: 5}
""",
            )

    def test_missing_duration_is_a_load_error_not_a_keyerror_later(self, tmp_path):
        from src.schemas import ConfigValidationError

        with pytest.raises(ConfigValidationError, match="duration_seconds"):
            self._load(tmp_path, "agent: {}\nfailures:\n  cpu: {enabled: true}\n")

    def test_defaults_applied_on_load(self, tmp_path):
        config = self._load(
            tmp_path,
            "agent: {}\nfailures:\n  cpu: {duration_seconds: 5}\n",
        )
        assert config.agent.interval_seconds == 10
        assert config.failures["cpu"]["cores"] == 1
        assert config.failures["cpu"]["enabled"] is False
        assert config.failures["cpu"]["probability"] == 0.0

    def test_logging_section_still_available(self, tmp_path):
        config = self._load(
            tmp_path,
            "agent: {}\nfailures: {}\nlogging: {level: DEBUG, format: json}\n",
        )
        assert config.get_logging_config()["level"] == "DEBUG"

    def test_missing_sections_keep_their_messages(self, tmp_path):
        with pytest.raises(ValueError, match="Missing required 'agent'"):
            self._load(tmp_path, "failures: {}\n")
        with pytest.raises(ValueError, match="Missing required 'failures'"):
            self._load(tmp_path, "agent: {}\n")


class TestValidateConfigWarnings:
    """validate_config() (warnings) is robust and behaves sensibly."""

    def test_clean_config_has_no_warnings(self):
        from src.config import validate_config

        assert validate_config(load_config("config.yaml")) == []

    def test_enabled_with_zero_probability_warns(self, tmp_path):
        from src.config import validate_config

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "agent: {}\nfailures:\n  cpu: {enabled: true, duration_seconds: 5}\n"
        )
        warnings = validate_config(load_config(str(config_file)))
        assert any("enabled but probability is 0" in w for w in warnings)

    def test_does_not_crash_on_unvalidated_garbage(self):
        """Config built by hand (bypassing load_config) must not raise."""
        from src.config import Config, validate_config

        garbage = Config(
            {
                "agent": {"interval_seconds": "abc"},
                "failures": {"cpu": {"enabled": True, "probability": "0.3"}},
                "logging": {"level": 5, "format": None},
            }
        )
        warnings = validate_config(garbage)
        assert any("not a number" in w for w in warnings)
