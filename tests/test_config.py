import pytest
from src.config import load_config


class TestConfigLoading:
    """Test configuration file loading."""

    def test_load_config_valid(self, tmp_path):
        """Test loading a valid config file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
agent:
  interval_seconds: 10
  dry_run: false
failures:
  cpu:
    enabled: true
    probability: 0.5
    duration_seconds: 5
    cores: 2
        """
        )

        config = load_config(str(config_file))
        assert config.agent.interval_seconds == 10
        assert config.agent.dry_run is False
        assert config.failures["cpu"]["enabled"] is True
        assert config.failures["cpu"]["cores"] == 2
        assert config.failures["cpu"]["probability"] == 0.5

    def test_load_config_with_dry_run(self, tmp_path):
        """Test config with dry_run enabled."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
agent:
  interval_seconds: 5
  dry_run: true
failures:
  cpu:
    enabled: true
    probability: 0.3
    duration_seconds: 2
    cores: 1
        """
        )

        config = load_config(str(config_file))
        assert config.agent.dry_run is True

    def test_load_config_missing_file(self):
        """Test handling of missing config file."""
        with pytest.raises(FileNotFoundError):
            load_config("nonexistent.yaml")

    def test_load_config_all_failures(self, tmp_path):
        """Test loading config with all failure types."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
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
        """
        )

        config = load_config(str(config_file))
        assert len(config.failures) == 4
        assert "cpu" in config.failures
        assert "memory" in config.failures
        assert "process" in config.failures
        assert "network" in config.failures

    def test_load_config_disabled_failures(self, tmp_path):
        """Test config with disabled failure types."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
agent:
  interval_seconds: 10
  dry_run: false
failures:
  cpu:
    enabled: false
    probability: 0.5
    duration_seconds: 5
    cores: 2
        """
        )

        config = load_config(str(config_file))
        assert config.failures["cpu"]["enabled"] is False
