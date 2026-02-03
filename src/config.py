"""
Configuration loading and validation for Py-Chaos-Agent.

Loads configuration from config.yaml and provides typed access to settings.
"""

import yaml
from typing import Any, Dict
from pathlib import Path


class AgentConfig:
    """Agent-level configuration."""
    
    def __init__(self, config: dict):
        self.interval_seconds = config.get('interval_seconds', 10)
        self.dry_run = config.get('dry_run', False)


class Config:
    """
    Main configuration object.
    
    Provides typed access to configuration values and preserves
    the raw config for modules that need it (like logging).
    """
    
    def __init__(self, config_dict: dict):
        # Store raw config for modules that need it
        self.raw_config = config_dict
        
        # Parse typed configs
        self.agent = AgentConfig(config_dict.get('agent', {}))
        self.failures = config_dict.get('failures', {})
    
    def get_logging_config(self) -> dict:
        """Get logging configuration from config."""
        return self.raw_config.get('logging', {})


def load_config(config_path: str = "config.yaml") -> Config:
    """
    Load configuration from YAML file.
    
    Args:
        config_path: Path to config.yaml file (default: "config.yaml")
    
    Returns:
        Config object with parsed configuration
    
    Raises:
        FileNotFoundError: If config file doesn't exist
        yaml.YAMLError: If config file is invalid YAML
        ValueError: If required configuration is missing
    """
    config_file = Path(config_path)
    
    if not config_file.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            f"Please create a config.yaml file or specify the correct path."
        )
    
    try:
        with open(config_file, 'r') as f:
            config_dict = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise yaml.YAMLError(
            f"Invalid YAML in configuration file: {config_path}\n"
            f"Error: {e}"
        )
    
    if config_dict is None:
        raise ValueError(f"Configuration file is empty: {config_path}")
    
    # Validate required sections
    if 'agent' not in config_dict:
        raise ValueError("Missing required 'agent' section in config.yaml")
    
    if 'failures' not in config_dict:
        raise ValueError("Missing required 'failures' section in config.yaml")
    
    return Config(config_dict)


def validate_config(config: Config) -> list[str]:
    """
    Validate configuration and return list of warnings.
    
    Args:
        config: Config object to validate
    
    Returns:
        List of warning messages (empty if no warnings)
    """
    warnings = []
    
    # Check agent config
    if config.agent.interval_seconds < 1:
        warnings.append("interval_seconds is less than 1 second, which may cause high CPU usage")
    
    if config.agent.interval_seconds > 300:
        warnings.append("interval_seconds is greater than 5 minutes, chaos may be infrequent")
    
    # Check failure configs
    for name, failure_config in config.failures.items():
        if not isinstance(failure_config, dict):
            warnings.append(f"Failure '{name}' configuration is not a dictionary")
            continue
        
        # Check probability
        prob = failure_config.get('probability', 0)
        if not 0 <= prob <= 1:
            warnings.append(f"Failure '{name}' probability {prob} is outside range [0, 1]")
        
        # Check if enabled but probability is 0
        if failure_config.get('enabled', False) and prob == 0:
            warnings.append(f"Failure '{name}' is enabled but probability is 0")
        
        # Specific validation for process killing
        if name == 'process' and failure_config.get('enabled', False):
            target_name = failure_config.get('target_name', '').lower()
            if not target_name:
                warnings.append("Process killing is enabled but no target_name specified")
            elif target_name in ['python', 'python3', 'java', 'node', 'systemd', 'init']:
                warnings.append(
                    f"Process target_name '{target_name}' is too generic and dangerous. "
                    f"Use a more specific application name."
                )
    
    # Check logging config
    logging_config = config.get_logging_config()
    if logging_config:
        log_level = logging_config.get('level', 'INFO').upper()
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if log_level not in valid_levels:
            warnings.append(
                f"Invalid log level '{log_level}'. "
                f"Must be one of: {', '.join(valid_levels)}"
            )
        
        log_format = logging_config.get('format', 'text').lower()
        if log_format not in ['text', 'json']:
            warnings.append(
                f"Invalid log format '{log_format}'. Must be 'text' or 'json'"
            )
    
    return warnings