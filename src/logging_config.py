import logging
import logging.handlers
import os
import sys
from typing import Optional

def setup_logging(
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
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
        enable_file_logging: If True, log to file in addition to console
        max_file_size_mb: Maximum size of each log file before rotation
        backup_count: Number of backup files to keep
    
    Environment Variables:
        LOG_LEVEL: Set logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        LOG_FILE: Path to log file
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
    
    # Determine if file logging is enabled
    if enable_file_logging:
        file_logging_env = os.environ.get('ENABLE_FILE_LOGGING', 'true').lower()
        enable_file_logging = file_logging_env not in ('false', '0', 'no')
    
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Remove any existing handlers
    root_logger.handlers.clear()
    
    # Console handler (stdout for INFO and below, stderr for WARNING and above)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
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
            file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
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