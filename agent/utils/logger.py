"""
Production Logger
Global logging configuration with JSON and colored console output
Supports both structured logging for cloud environments and human-readable console logs
"""

import logging
import json
import sys
import os
from datetime import datetime
from typing import Any

# Handle optional import for colored output
try:
    from agent.config import settings
except (ImportError, RuntimeError):
    # Fallback settings if config can't be imported yet
    class FallbackSettings:
        log_level = "DEBUG"
        log_format = "text"
    settings = FallbackSettings()


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging - ideal for cloud environments"""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(log_data)


class ColoredFormatter(logging.Formatter):
    """Colored formatter for human-readable console output"""
    
    # Color codes for different log levels
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
    }
    RESET = '\033[0m'
    BOLD = '\033[1m'
    
    def format(self, record: logging.LogRecord) -> str:
        # Color the level name
        level_name = record.levelname
        color = self.COLORS.get(level_name, self.RESET)
        
        # Format the log message
        log_message = (
            f"{self.BOLD}[{record.levelname}]{self.RESET} "
            f"{record.name}:{record.funcName}:{record.lineno:<3} - "
            f"{record.getMessage()}"
        )
        
        # Add exception info if present
        if record.exc_info:
            log_message += f"\n{self.formatException(record.exc_info)}"
        
        return log_message


class SectionFormatter(logging.Formatter):
    """Formatter for section headers and startup messages"""
    
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


# Configure root logger once at startup
_configured = False

def configure_logging():
    """Configure global logging - call once at startup"""
    global _configured
    
    if _configured:
        return
    
    try:
        log_level = getattr(settings, 'log_level', 'DEBUG')
        log_format = getattr(settings, 'log_format', 'text')
    except:
        log_level = 'DEBUG'
        log_format = 'text'
    
    # Get root logger and force DEBUG level to catch everything
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Force DEBUG to capture all
    
    # Remove existing handlers to avoid duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Console handler — set to the configured level but root is DEBUG
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level.upper(), logging.DEBUG))
    
    # Choose formatter based on configuration
    if log_format == "json":
        formatter = JSONFormatter()
    else:
        formatter = ColoredFormatter()
    
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Add file handler as failsafe — writes to logs/agent.log
    try:
        os.makedirs("logs", exist_ok=True)
        file_handler = logging.FileHandler("logs/agent.log", mode='a', encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)  # Always capture everything to file
        file_formatter = ColoredFormatter()  # Use colored format for file too for readability
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)
    except Exception as e:
        print(f"Warning: Could not create file logger: {e}")
    
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """
    Get configured logger for a module.
    Automatically configures global logging on first call.
    
    Args:
        name: Logger name (typically __name__)
        
    Returns:
        logging.Logger: Configured logger instance
    """
    # Ensure global logging is configured
    configure_logging()
    
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # Force DEBUG level
    
    return logger


def get_section_logger() -> logging.Logger:
    """Get logger for startup/section messages (no color/structure)"""
    configure_logging()
    
    logger = logging.getLogger("pipecat-agent.section")
    
    # Use section formatter for cleaner startup output
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(SectionFormatter())
        logger.addHandler(handler)
    
    return logger


# Initialize main logger
logger = get_logger("pipecat-agent")