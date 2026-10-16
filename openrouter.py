"""OpenRouter Multi-LLM API client for AirZen CRM.

Provides unified asynchronous chat completions and text generation across
multiple LLM providers (Anthropic Claude, OpenAI GPT-4o, Google Gemini, DeepSeek, etc.)
with automatic retries, fallbacks, and timeout management.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional
import aiohttp

logger = logging.getLogger("openrouter")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "google/gemini-2.0-flash-001"
FALLBACK_OPENROUTER_MODEL = "openai/gpt-4o-mini"


async def get_openrouter_api_key() -> str:
    from db import get_setting
    try:
        db_key = await get_setting("OPENROUTER_API_KEY", "")
        if db_key and db_key.strip():
            return db_key.strip()
    except Exception:
        pass
    return os.getenv("OPENROUTER_API_KEY", "").strip()


async def get_openrouter_default_model() -> str:
    from db import get_setting
    try:
        db_model = await get_setting("OPENROUTER_DEFAULT_MODEL", "")
        if db_model and db_model.strip():
            return db_model.strip()
    except Exception:
        pass
    return os.getenv("OPENROUTER_DEFAULT_MODEL", DEFAULT_OPENROUTER_MODEL).strip()


async def openrouter_chat_completion(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    system_prompt: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 1000,
    timeout_seconds: float = 30.0,
) -> Dict[str, Any]:
    """Execute chat completion via OpenRouter."""
    api_key = await get_openrouter_api_key()
    if not api_key:
        return {
            "success": False,
            "reply": None,
            "error": "OPENROUTER_API_KEY not configured",
            "reason": "missing_api_key",
        }

    selected_model = model or await get_openrouter_default_model()
    
    formatted_messages = []
    if system_prompt:
        formatted_messages.append({"role": "system", "content": system_prompt})
    formatted_messages.extend(messages)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://voice.airzensg.com",
        "X-Title": "AirZen CRM",
        "Content-Type": "application/json",
    }

    payload = {
        "model": selected_model,
        "messages": formatted_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    url = f"{OPENROUTER_BASE_URL}/chat/completions"

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logger.warning("OpenRouter error HTTP %d: %s", resp.status, err_text[:300])
                    
                    # Fallback attempt if first model fails
                    if selected_model != FALLBACK_OPENROUTER_MODEL:
                        logger.info("Attempting fallback model: %s", FALLBACK_OPENROUTER_MODEL)
                        payload["model"] = FALLBACK_OPENROUTER_MODEL
                        async with session.post(url, headers=headers, json=payload) as fb_resp:
                            if fb_resp.status == 200:
                                data = await fb_resp.json()
                                choice = (data.get("choices") or [{}])[0]
                                content = choice.get("message", {}).get("content", "")
                                return {
                                    "success": True,
                                    "reply": content.strip(),
                                    "model": FALLBACK_OPENROUTER_MODEL,
                                    "is_fallback": True,
                                }

                    return {
                        "success": False,
                        "reply": None,
                        "error": f"OpenRouter HTTP {resp.status}: {err_text[:200]}",
                        "reason": "provider_error",
                    }

                data = await resp.json()
                choice = (data.get("choices") or [{}])[0]
                content = choice.get("message", {}).get("content", "")
                return {
                    "success": True,
                    "reply": content.strip(),
                    "model": selected_model,
                    "usage": data.get("usage"),
                }
    except asyncio.TimeoutError:
        return {"success": False, "reply": None, "error": "Request timed out", "reason": "timeout"}
    except Exception as exc:
        logger.error("OpenRouter request exception: %s", exc)
        return {"success": False, "reply": None, "error": str(exc)[:300], "reason": "exception"}
