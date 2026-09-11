"""AI-based anti-bot challenge solver via vision API (user's local OpenAI proxy).

Uses screenshot → vision model → structured solve instructions.
No extra dependencies beyond httpx (already installed).
Supports SSE streaming responses (9router proxy workaround).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger("solver")

DEFAULT_AI_BASE_URL = "http://localhost:20128/v1"
DEFAULT_AI_MODEL = "Free"

_SOLVER_SYSTEM = (
    "You are a CAPTCHA / anti-bot challenge solver. Analyze the screenshot.\n"
    "Respond ONLY with valid JSON (no markdown, no code fences):\n"
    '{"type":"text_captcha"|"image_select"|"checkbox"|"turnstile"|"none"|"error",'
    '"text":"","fill_selector":"","click_selectors":[],"description":""}\n'
    "Rules:\n"
    "- text_captcha: visible text/numbers to type into input. Provide text + fill_selector.\n"
    "- image_select: images to click (e.g. 'select all traffic lights'). Provide click_selectors.\n"
    "- checkbox: 'I am not a robot' checkbox. Provide fill_selector with its CSS selector.\n"
    "- turnstile: Cloudflare Turnstile widget (iframe). Provide click_selectors with iframe selector.\n"
    "- none: no challenge visible.\n"
    "- error: AI could not determine. description explains.\n"
    "CSS selectors: prefer #id, [data-testid], [name], or unique class. Be precise.\n"
    "If text CAPTCHA has obfuscated/distorted text, do your best to read it."
)


async def solve_challenge_screenshot(
    image_bytes: bytes,
    *,
    hint: str = "",
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Send screenshot to vision AI, return structured solve instructions.

    Returns dict with keys:
      type: text_captcha|image_select|checkbox|turnstile|none|error
      text: extracted text (text_captcha)
      fill_selector: CSS selector for input field
      click_selectors: list of CSS selectors to click
      description: human-readable explanation
    """
    _base = (base_url or DEFAULT_AI_BASE_URL).rstrip("/")
    _model = model or DEFAULT_AI_MODEL



    b64 = base64.b64encode(image_bytes).decode("utf-8")

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout + 15.0)) as client:
        messages = [
            {"role": "system", "content": _SOLVER_SYSTEM},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Solve this. {hint}" if hint else "Solve any anti-bot challenge visible.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ]
        resp = await client.post(
            f"{_base}/chat/completions",
            json={
                "model": _model,
                "messages": messages,
                "max_tokens": 2048,
                "temperature": 0.1,
                # Stream: false explicitly to avoid SSE on most backends
                "stream": False,
            },
        )
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if content_type.startswith("text/event-stream"):
            # proxy forces SSE; parse from event stream
            content = _parse_sse_content(resp.text)
        else:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]

    return _parse_json_response(content)


def _parse_sse_content(text: str) -> str:
    """Extract content from SSE response lines (data: ...)."""
    full = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                if delta.get("content"):
                    full += delta["content"]
            except json.JSONDecodeError:
                pass
    return full.strip()


def _parse_json_response(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"type": "error", "description": text[:500]}


async def test_solver(base_url: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Quick connectivity test — sends minimal image and checks response shape."""
    dummy = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    result = await solve_challenge_screenshot(dummy, hint="test", base_url=base_url, model=model)
    return result
