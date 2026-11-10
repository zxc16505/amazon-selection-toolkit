# -*- coding: utf-8 -*-
"""站点配置、常量、路径。外置到 YAML 方便调整不碰代码。"""
from __future__ import annotations

import yaml
from pathlib import Path
from dataclasses import dataclass


BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
DB = DATA / "ranks.sqlite3"
COOKIE_FILE = DATA / "cookies.json"
CACHE_FILE = DATA / "http_cache.json"


@dataclass(frozen=True)
class Market:
    code: str
    host: str
    currency: str
    accept_language: str


@dataclass(frozen=True)
class Config:
    markets: dict[str, Market]
    user_agent: str
    block_markers: tuple[str, ...]
    block_max_len: int
    max_attempts: int
    backoff_seconds: tuple[int, ...]

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        if path is None:
            path = BASE / "config.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        markets = {
            code: Market(code=code, host=m["host"], currency=m["currency"],
                         accept_language=m["accept_language"])
            for code, m in data["markets"].items()
        }
        return cls(
            markets=markets,
            user_agent=data["user_agent"],
            block_markers=tuple(data["block_markers"]),
            block_max_len=data["block_max_len"],
            max_attempts=data["max_attempts"],
            backoff_seconds=tuple(data["backoff_seconds"]),
        )


PATH_OF = {"bestsellers": "/gp/bestsellers/{}", "new-releases": "/gp/new-releases/{}"}
