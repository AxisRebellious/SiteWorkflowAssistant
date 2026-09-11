"""Persistence for workflows and notifier settings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent / "data" / "config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "bale": {"bot_token": "", "chat_id": ""},
    "poll_interval_seconds": 60,
    "workflows": [],
    "scenario_queue": [],
    "monitoring_enabled": False,
}


def _ensure_parent() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    _ensure_parent()
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG.copy())
        return json.loads(json.dumps(DEFAULT_CONFIG))
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    data = json.loads(raw)
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(data)

    bl = {**DEFAULT_CONFIG["bale"], **(data.get("bale") or {})} if isinstance(data.get("bale"), dict) else dict(DEFAULT_CONFIG["bale"])

    # مهاجرت از eitaa (توکن → bot_token)
    ea = data.get("eitaa") if isinstance(data.get("eitaa"), dict) else {}
    if not (bl.get("bot_token") or "").strip() and (ea.get("token") or "").strip():
        bl["bot_token"] = str(ea.get("token") or "").strip()
    if not (bl.get("chat_id") or "").strip() and ea.get("chat_id") is not None:
        bl["chat_id"] = str(ea.get("chat_id") or "").strip()

    # مهاجرت از telegram
    tg = data.get("telegram") if isinstance(data.get("telegram"), dict) else {}
    if not (bl.get("bot_token") or "").strip() and tg.get("bot_token"):
        bl["bot_token"] = str(tg.get("bot_token") or "").strip()
    if not (bl.get("chat_id") or "").strip() and tg.get("chat_id") is not None:
        bl["chat_id"] = str(tg.get("chat_id") or "").strip()

    merged["bale"] = bl
    sq = data.get("scenario_queue")
    merged["scenario_queue"] = list(sq) if isinstance(sq, list) else []
    return merged


def save_config(cfg: dict[str, Any]) -> None:
    _ensure_parent()
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
