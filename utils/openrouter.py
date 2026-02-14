"""
OpenRouter API helpers.
"""

import json
from typing import Any, Dict, Optional, Tuple

import aiohttp

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_REFERER = "https://github.com/bredsky212/Logiq212/"
OPENROUTER_TITLE = "Logiq212"


def _build_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_REFERER,
        "X-Title": OPENROUTER_TITLE,
    }


async def request_json(
    method: str,
    path: str,
    api_key: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Optional[Dict[str, Any]], str, aiohttp.typedefs.LooseHeaders]:
    """Send an OpenRouter request and return status, JSON (if any), raw text, and headers."""
    url = f"{OPENROUTER_BASE_URL}{path}"
    headers = _build_headers(api_key)
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, json=payload) as response:
            text = await response.text()
            data = None
            if text:
                try:
                    data = json.loads(text)
                except Exception:
                    data = None
            return response.status, data, text, response.headers
