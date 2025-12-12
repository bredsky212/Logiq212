"""
Logging utility for Logiq
Configures structured logging with file and console output
"""

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional


def setup_logger(
    name: str = "Logiq",
    level: str = "INFO",
    log_file: Optional[str] = "logs/bot.log",
    log_format: str = "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    date_format: str = "%Y-%m-%d %H:%M:%S"
) -> logging.Logger:
    """
    Setup and configure logger

    Args:
        name: Logger name
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Path to log file (None for console only)
        log_format: Log message format
        date_format: Date format for timestamps

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    logger.handlers.clear()

    # Create formatter
    formatter = logging.Formatter(log_format, datefmt=date_format)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


class BotLogger:
    """Centralized logger for bot operations"""

    def __init__(self, config: dict):
        """Initialize bot logger with configuration"""
        self.logger = setup_logger(
            level=config.get("level", "INFO"),
            log_file=config.get("file", "logs/bot.log"),
            log_format=config.get("format", "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"),
            date_format=config.get("date_format", "%Y-%m-%d %H:%M:%S")
        )

        # Ensure other module loggers propagate to handlers
        root = logging.getLogger()
        root.setLevel(self.logger.level)
        for handler in self.logger.handlers:
            if handler not in root.handlers:
                root.addHandler(handler)

    def debug(self, message: str) -> None:
        """Log debug message"""
        self.logger.debug(message)

    def info(self, message: str) -> None:
        """Log info message"""
        self.logger.info(message)

    def warning(self, message: str) -> None:
        """Log warning message"""
        self.logger.warning(message)

    def error(self, message: str, exc_info: bool = False) -> None:
        """Log error message"""
        self.logger.error(message, exc_info=exc_info)

    def critical(self, message: str, exc_info: bool = False) -> None:
        """Log critical message"""
        self.logger.critical(message, exc_info=exc_info)

    def command(self, user: str, command: str, guild: str) -> None:
        """Log command execution"""
        self.info(f"Command '{command}' executed by {user} in {guild}")

    def event(self, event_name: str, details: str = "") -> None:
        """Log event"""
        self.info(f"Event '{event_name}': {details}")

    def cog_load(self, cog_name: str) -> None:
        """Log cog loading"""
        self.info(f"Loaded cog: {cog_name}")

    def cog_unload(self, cog_name: str) -> None:
        """Log cog unloading"""
        self.info(f"Unloaded cog: {cog_name}")
