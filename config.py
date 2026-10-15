"""Config loading. Flat YAML, unknown keys are rejected so typos surface early."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

import yaml


@dataclass
class Friend:
    name: str = ""
    note: str = ""


@dataclass
class Config:
    openrouter_api_key: str = ""
    model: str = "anthropic/claude-sonnet-4.5"
    base_url: str = "https://openrouter.ai/api/v1"

    # bot is the WebSocket *server*; NapCat connects to it as a WS client
    listen_host: str = "127.0.0.1"
    listen_port: int = 6700
    onebot_token: str = ""

    persona_file: str = "persona.md"
    db_path: str = "data/chat.db"
    friends: dict[int, Friend] = field(default_factory=dict)

    start_enabled: bool = True
    takeover_minutes: float = 30
    quiet_hours: str = ""  # e.g. "00:30-08:00"; empty disables
    debounce_seconds: float = 3
    humanize: bool = True  # typing delays; tests turn this off

    history_window: int = 30
    max_segments: int = 4
    max_tokens: int = 400
    temperature: float = 0.9

    rate_per_user_per_min: int = 6
    rate_global_per_min: int = 15


def load(path: str = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"config.yaml 里有无法识别的配置项: {sorted(unknown)}")

    friends: dict[int, Friend] = {}
    for qq, v in (raw.pop("friends", None) or {}).items():
        if isinstance(v, str):
            v = {"name": v}
        friends[int(qq)] = Friend(**(v or {}))

    cfg = Config(friends=friends, **raw)
    cfg.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY") or cfg.openrouter_api_key
    return cfg
