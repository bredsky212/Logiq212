"""
Throttled denial logging utility.
"""

import logging
import time
from typing import Tuple, Dict

logger = logging.getLogger(__name__)


class DenialLogger:
    """Throttle denial logs per (guild, user, command, feature) within a window."""

    def __init__(self, window_seconds: int = 60):
        self.window = window_seconds
        self.cache: Dict[Tuple[int, int, str, str], float] = {}

    def should_log(self, guild_id: int, user_id: int, command: str, feature: str) -> bool:
        key = (guild_id, user_id, command, feature)
        now = time.time()
        last = self.cache.get(key, 0)
        if now - last > self.window:
            self.cache[key] = now
            return True
        return False
