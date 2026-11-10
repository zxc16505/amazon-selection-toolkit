# -*- coding: utf-8 -*-
"""SQLite 存储层。"""
from __future__ import annotations

import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS ranks (
    run_date   TEXT NOT NULL,
    market     TEXT NOT NULL,
    list_type  TEXT NOT NULL,
    category   TEXT NOT NULL,
    rank       INTEGER NOT NULL,
    asin       TEXT NOT NULL,
    title      TEXT,
    price      REAL,
    currency   TEXT,
    rating     REAL,
    reviews    INTEGER,
    PRIMARY KEY (run_date, market, list_type, category, asin));

CREATE TABLE IF NOT EXISTS request_log (
    ts         TEXT NOT NULL,
    market     TEXT, list_type TEXT, category TEXT,
    status     TEXT, http INTEGER, ms INTEGER, note TEXT);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def already_done(con: sqlite3.Connection, run_date: str, market: str,
                 list_type: str, category: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM ranks WHERE run_date=? AND market=? AND list_type=? "
        "AND category=? LIMIT 1",
        (run_date, market, list_type, category)).fetchone()
    return row is not None


def save(con: sqlite3.Connection, run_date: str, market: str, list_type: str,
         category: str, rows: list[dict]) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO ranks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(run_date, market, list_type, category, r["rank"], r["asin"], r["title"],
          r["price"], r["currency"], r["rating"], r["reviews"]) for r in rows])


def previous(con: sqlite3.Connection, market: str, list_type: str,
             category: str, run_date: str) -> dict[str, dict] | None:
    prev = con.execute(
        "SELECT MAX(run_date) FROM ranks WHERE market=? AND list_type=? "
        "AND category=? AND run_date<?",
        (market, list_type, category, run_date)).fetchone()[0]
    if not prev:
        return None
    return {r[0]: {"rank": r[1], "price": r[2], "title": r[3]}
            for r in con.execute(
                "SELECT asin, rank, price, title FROM ranks WHERE run_date=? "
                "AND market=? AND list_type=? AND category=?",
                (prev, market, list_type, category))}


def log_request(con: sqlite3.Connection, ts: str, market: str, list_type: str,
                category: str, status: str, http: int | None, ms: int, note: str) -> None:
    con.execute(
        "INSERT INTO request_log VALUES (?,?,?,?,?,?,?,?)",
        (ts, market, list_type, category, status, http, ms, note))
