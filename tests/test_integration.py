"""Integration tests combining multiple components."""

import logging
from src.config import load_config
from src.failures.cpu import inject_cpu
from src.failures.memory import inject_memory
from src.failures.process import inject_process


class TestIntegration:
    """Integration tests for multiple failure modes."""

    def test_multiple_dry_run_injections(self, caplog):
        """Test running multiple failure injections in dry run mode."""
        caplog.set_level(logging.INFO)
        configs = [
            ("cpu", {"duration_seconds": 1, "cores": 2}),
            ("memory", {"duration_seconds": 1, "mb": 50}),
            ("process", {"target_name": "test"}),
        ]

        for failure_type, config in configs:
            if failure_type == "cpu":
                inject_cpu(config, dry_run=True)
            elif failure_type == "memory":
                inject_memory(config, dry_run=True)
            elif failure_type == "process":
                inject_process(config, dry_run=True)

        # Count dry run messages
        dry_run_count = sum(
            1 for record in caplog.records if "DRY RUN" in record.message
        )
        assert dry_run_count >= 2

    def test_config_and_injection_integration(self, tmp_path, caplog):
        """Test loading config and using it for injection."""
        caplog.set_level(logging.INFO)
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
agent:
  interval_seconds: 10
  dry_run: true
failures:
  cpu:
    enabled: true
    probability: 0.5
    duration_seconds: 2
    cores: 1
        """)

        config = load_config(str(config_file))
        cpu_config = config.failures["cpu"]

        inject_cpu(cpu_config, dry_run=config.agent.dry_run)

        # Check for expected messages
        log_messages = " ".join([record.message for record in caplog.records])
        assert "DRY RUN" in log_messages
        # Check that CPU injection occurred
        assert "CPU injection" in log_messages

    def test_multiple_failure_types_from_config(self, tmp_path):
        """Test loading and validating config with multiple failure types."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
agent:
  interval_seconds: 15
  dry_run: true
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
    enabled: false
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

        # Verify all failure types are loaded
        assert config.failures["cpu"]["enabled"] is True
        assert config.failures["memory"]["enabled"] is True
        assert config.failures["process"]["enabled"] is False
        assert config.failures["network"]["enabled"] is True

        # Verify specific configurations
        assert config.failures["cpu"]["cores"] == 2
        assert config.failures["memory"]["mb"] == 200
        assert config.failures["network"]["delay_ms"] == 300

    def test_sequential_injections(self, caplog):
        """Test running injections sequentially."""
        caplog.set_level(logging.INFO)

        # Run CPU injection
        cpu_config = {"duration_seconds": 1, "cores": 1}
        inject_cpu(cpu_config, dry_run=True)

        # Run memory injection
        mem_config = {"duration_seconds": 1, "mb": 50}
        inject_memory(mem_config, dry_run=True)

        # Should have references to both CPU and memory
        has_cpu = any("cpu" in record.message.lower() for record in caplog.records)
        has_memory = any(
            "memory" in record.message.lower() for record in caplog.records
        )
        assert has_cpu and has_memory
