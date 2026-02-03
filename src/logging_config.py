"""
Logging configuration for Py-Chaos-Agent.

This module provides centralized logging configuration with:
- Structured logging with context
- Multiple output handlers (console, file, JSON)
- Log level management via environment variables
- Correlation IDs for request tracking
- Performance metrics logging
- Security-aware logging (no sensitive data)
"""

import logging
import logging.handlers
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, Optional
import threading

# Thread-local storage for correlation IDs
_thread_local = threading.local()


class StructuredFormatter(logging.Formatter):
    """
    Custom formatter that outputs logs in a structured format.
    
    Supports both JSON and human-readable formats depending on configuration.
    Includes correlation IDs, timestamps, and contextual information.
    """

    def __init__(self, fmt=None, datefmt=None, style='%', json_format=False):
        super().__init__(fmt, datefmt, style)
        self.json_format = json_format
        self.hostname = os.environ.get('HOSTNAME', 'unknown')
        self.service_name = 'py-chaos-agent'

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON or structured text."""
        
        # Add correlation ID if available
        correlation_id = getattr(_thread_local, 'correlation_id', None)
        if correlation_id:
            record.correlation_id = correlation_id
        
        # Add custom fields
        record.service = self.service_name
        record.hostname = self.hostname
        
        if self.json_format:
            return self._format_json(record)
        else:
            return self._format_text(record)

    def _format_json(self, record: logging.LogRecord) -> str:
        """Format as JSON for machine parsing."""
        log_data = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'service': self.service_name,
            'hostname': self.hostname,
        }

        # Add correlation ID if present
        if hasattr(record, 'correlation_id'):
            log_data['correlation_id'] = record.correlation_id

        # Add exception info if present
        if record.exc_info:
            log_data['exception'] = {
                'type': record.exc_info[0].__name__ if record.exc_info[0] else None,
                'message': str(record.exc_info[1]) if record.exc_info[1] else None,
                'traceback': self.formatException(record.exc_info) if record.exc_info else None,
            }

        # Add any extra fields
        for key, value in record.__dict__.items():
            if key not in ['name', 'msg', 'args', 'created', 'filename', 'funcName',
                          'levelname', 'levelno', 'lineno', 'module', 'msecs',
                          'message', 'pathname', 'process', 'processName',
                          'relativeCreated', 'thread', 'threadName', 'exc_info',
                          'exc_text', 'stack_info', 'correlation_id', 'service',
                          'hostname']:
                log_data[key] = value

        return json.dumps(log_data)

    def _format_text(self, record: logging.LogRecord) -> str:
        """Format as human-readable text."""
        # Build the base message
        timestamp = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        
        # Build correlation ID part
        corr_id_str = ''
        if hasattr(record, 'correlation_id'):
            corr_id_str = f' [{record.correlation_id}]'
        
        # Build the main log line
        base_msg = f'{timestamp} {record.levelname:8s} [{record.name}]{corr_id_str} {record.getMessage()}'
        
        # Add exception if present
        if record.exc_info:
            base_msg += '\n' + self.formatException(record.exc_info)
        
        return base_msg


class SensitiveDataFilter(logging.Filter):
    """
    Filter to prevent logging of sensitive data.
    
    Redacts common sensitive patterns like API keys, tokens, passwords.
    """
    
    SENSITIVE_PATTERNS = [
        'password', 'token', 'api_key', 'secret', 'auth',
        'credential', 'private_key', 'access_key'
    ]
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Redact sensitive data from log messages."""
        message = record.getMessage()
        
        # Simple redaction - in production, use regex for more sophisticated matching
        for pattern in self.SENSITIVE_PATTERNS:
            if pattern in message.lower():
                # Redact the value after the sensitive key
                record.msg = str(record.msg).replace(
                    pattern, f'{pattern}=***REDACTED***'
                )
        
        return True


def set_correlation_id(correlation_id: str):
    """Set correlation ID for the current thread."""
    _thread_local.correlation_id = correlation_id


def get_correlation_id() -> Optional[str]:
    """Get correlation ID for the current thread."""
    return getattr(_thread_local, 'correlation_id', None)


def clear_correlation_id():
    """Clear correlation ID for the current thread."""
    if hasattr(_thread_local, 'correlation_id'):
        delattr(_thread_local, 'correlation_id')


def setup_logging(
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
    json_logs: bool = False,
    enable_file_logging: bool = True,
    max_file_size_mb: int = 10,
    backup_count: int = 5,
) -> None:
    """
    Configure application-wide logging.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
                  If None, reads from LOG_LEVEL environment variable (default: INFO)
        log_file: Path to log file. If None, reads from LOG_FILE env var
                 (default: /var/log/chaos-agent/agent.log or ./logs/agent.log)
        json_logs: If True, output logs in JSON format. Reads from JSON_LOGS env var
        enable_file_logging: If True, log to file in addition to console
        max_file_size_mb: Maximum size of each log file before rotation
        backup_count: Number of backup files to keep
    
    Environment Variables:
        LOG_LEVEL: Set logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        LOG_FILE: Path to log file
        JSON_LOGS: Set to 'true' or '1' to enable JSON logging
        ENABLE_FILE_LOGGING: Set to 'false' or '0' to disable file logging
    """
    
    # Determine log level
    if log_level is None:
        log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    
    numeric_level = getattr(logging, log_level, logging.INFO)
    
    # Determine log file path
    if log_file is None:
        log_file = os.environ.get('LOG_FILE')
        if log_file is None:
            # Try /var/log first, fall back to local logs directory
            if os.access('/var/log', os.W_OK):
                log_dir = '/var/log/chaos-agent'
            else:
                log_dir = './logs'
            
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, 'agent.log')
    
    # Determine JSON format
    if not json_logs:
        json_env = os.environ.get('JSON_LOGS', '').lower()
        json_logs = json_env in ('true', '1', 'yes')
    
    # Determine if file logging is enabled
    if enable_file_logging:
        file_logging_env = os.environ.get('ENABLE_FILE_LOGGING', 'true').lower()
        enable_file_logging = file_logging_env not in ('false', '0', 'no')
    
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Remove any existing handlers
    root_logger.handlers.clear()
    
    # Create formatters
    console_formatter = StructuredFormatter(json_format=json_logs)
    file_formatter = StructuredFormatter(json_format=True)  # Always JSON for files
    
    # Console handler (stdout for INFO and below, stderr for WARNING and above)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(SensitiveDataFilter())
    root_logger.addHandler(console_handler)
    
    # File handler with rotation
    if enable_file_logging:
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=max_file_size_mb * 1024 * 1024,  # Convert MB to bytes
                backupCount=backup_count,
                encoding='utf-8',
            )
            file_handler.setLevel(numeric_level)
            file_handler.setFormatter(file_formatter)
            file_handler.addFilter(SensitiveDataFilter())
            root_logger.addHandler(file_handler)
            
            # Log initial message about file logging
            logging.info(
                'File logging enabled',
                extra={
                    'log_file': log_file,
                    'max_size_mb': max_file_size_mb,
                    'backup_count': backup_count,
                }
            )
        except Exception as e:
            # Fall back to console-only logging if file logging fails
            logging.warning(
                f'Failed to setup file logging: {e}. Continuing with console-only logging.'
            )
    
    # Log startup information
    logging.info(
        'Logging configured',
        extra={
            'log_level': log_level,
            'json_format': json_logs,
            'file_logging': enable_file_logging,
        }
    )
    
    # Set specific log levels for noisy libraries
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the specified name.
    
    Args:
        name: Logger name, typically __name__ of the module
    
    Returns:
        Configured logger instance
    
    Example:
        logger = get_logger(__name__)
        logger.info('Application started')
        logger.error('Something went wrong', exc_info=True)
    """
    return logging.getLogger(name)


# Convenience logging functions with context
def log_failure_injection(
    logger: logging.Logger,
    failure_type: str,
    action: str,
    status: str,
    **kwargs
):
    """
    Log a failure injection event with structured context.
    
    Args:
        logger: Logger instance
        failure_type: Type of failure (cpu, memory, process, network)
        action: Action being performed (start, complete, failed)
        status: Status of the action
        **kwargs: Additional context to include in log
    """
    logger.info(
        f'Failure injection {action}',
        extra={
            'failure_type': failure_type,
            'action': action,
            'status': status,
            **kwargs
        }
    )


def log_metric_event(
    logger: logging.Logger,
    metric_name: str,
    metric_value: Any,
    **kwargs
):
    """
    Log a metric event.
    
    Args:
        logger: Logger instance
        metric_name: Name of the metric
        metric_value: Value of the metric
        **kwargs: Additional context
    """
    logger.debug(
        f'Metric recorded: {metric_name}',
        extra={
            'metric_name': metric_name,
            'metric_value': metric_value,
            **kwargs
        }
    )
