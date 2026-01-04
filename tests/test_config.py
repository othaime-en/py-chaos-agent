from src.config import load_config

def test_load_config(tmp_path):
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
    assert config.failures['cpu']['enabled'] is True
    assert config.failures['cpu']['cores'] == 2