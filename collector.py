#!/usr/bin/env python3
"""
Legia / Roboticket inventory collector v0.1

Purpose:
- Open the public Roboticket stadium page in a real browser.
- Intercept selected WGL XHR responses.
- Save raw responses and normalized records to SQLite.
- Avoid reverse-engineering Roboticket's dynamic vaoKeysForCache parameter.

This is an experimental collector for public data visible in the browser.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright, Response

CAPTURE_MARKERS = (
    "GetWGLSeats?",
    "GetWGLSeatsOccInfo?",
    "GetWGLSeatsMyInfo?",
    "GetWGLSectorsInfo?",
    "GetWGLSectorsInfoExt?",
    "GetWGLSeatsInfo?",
    "GetWGLSeatsInfoExt?",
)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL,
    captured_at_utc TEXT NOT NULL,
    page_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    request_url TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    body_sha256 TEXT NOT NULL,
    body_json TEXT,
    FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS seats (
    event_id INTEGER NOT NULL,
    seat_id INTEGER NOT NULL,
    sector_id INTEGER,
    row_label TEXT,
    seat_label TEXT,
    pa_id INTEGER,
    x INTEGER,
    y INTEGER,
    angle INTEGER,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    PRIMARY KEY(event_id, seat_id)
);

CREATE TABLE IF NOT EXISTS seat_occupancy (
    snapshot_id TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    seat_id INTEGER NOT NULL,
    occ INTEGER,
    any_right INTEGER,
    has_sg_right INTEGER,
    has_res_right INTEGER,
    source_url TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, seat_id, source_url)
);

CREATE TABLE IF NOT EXISTS my_seats (
    snapshot_id TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    seat_id INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, seat_id)
);

CREATE INDEX IF NOT EXISTS ix_occ_event_seat_time
ON seat_occupancy(event_id, seat_id, captured_at_utc);
"""

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def event_id_from_url(url: str) -> int | None:
    try:
        q = parse_qs(urlparse(url).query)
        value = q.get("eventId", [None])[0]
        return int(value) if value is not None else None
    except Exception:
        return None

def endpoint_name(url: str) -> str:
    return urlparse(url).path.rsplit("/", 1)[-1]

def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

class Store:
    def __init__(self, db_path: Path, raw_dir: Path):
        self.db_path = db_path
        self.raw_dir = raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def begin_snapshot(self, snapshot_id: str, event_id: int, page_url: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots(snapshot_id,event_id,captured_at_utc,page_url) VALUES(?,?,?,?)",
            (snapshot_id, event_id, utc_now(), page_url),
        )
        self.conn.commit()

    def save_response(
        self,
        snapshot_id: str,
        event_id: int,
        response: Response,
        data: Any,
        raw_text: str,
    ):
        ts = utc_now()
        url = response.url
        endpoint = endpoint_name(url)
        body_hash = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()

        self.conn.execute(
            """INSERT INTO raw_responses
               (snapshot_id,captured_at_utc,endpoint,request_url,status_code,body_sha256,body_json)
               VALUES(?,?,?,?,?,?,?)""",
            (snapshot_id, ts, endpoint, url, response.status, body_hash, raw_text),
        )

        safe_ts = ts.replace(":", "-").replace("+", "_")
        raw_path = self.raw_dir / f"{snapshot_id}_{safe_ts}_{endpoint}_{body_hash[:10]}.json"
        raw_path.write_text(raw_text, encoding="utf-8")

        if endpoint == "GetWGLSeats":
            self._save_seats(event_id, ts, data)
        elif endpoint == "GetWGLSeatsOccInfo":
            self._save_occ(snapshot_id, event_id, ts, url, data)
        elif endpoint == "GetWGLSeatsMyInfo":
            self._save_my(snapshot_id, event_id, ts, url, data)

        self.conn.commit()

    def _save_seats(self, event_id: int, ts: str, data: Any):
        rows = data.get("seats", []) if isinstance(data, dict) else []
        for s in rows:
            if not isinstance(s, dict) or "id" not in s:
                continue
            self.conn.execute(
                """INSERT INTO seats
                   (event_id,seat_id,sector_id,row_label,seat_label,pa_id,x,y,angle,first_seen_utc,last_seen_utc)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_id,seat_id) DO UPDATE SET
                     sector_id=excluded.sector_id,
                     row_label=excluded.row_label,
                     seat_label=excluded.seat_label,
                     pa_id=excluded.pa_id,
                     x=excluded.x,
                     y=excluded.y,
                     angle=excluded.angle,
                     last_seen_utc=excluded.last_seen_utc""",
                (
                    event_id, s.get("id"), s.get("sectorId"), s.get("row"),
                    s.get("label"), s.get("paId"), s.get("x"), s.get("y"),
                    s.get("a"), ts, ts
                ),
            )

    def _save_occ(self, snapshot_id: str, event_id: int, ts: str, url: str, data: Any):
        if not isinstance(data, list):
            return
        for s in data:
            if not isinstance(s, dict) or "id" not in s:
                continue
            self.conn.execute(
                """INSERT OR REPLACE INTO seat_occupancy
                   (snapshot_id,captured_at_utc,event_id,seat_id,occ,any_right,has_sg_right,has_res_right,source_url)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id, ts, event_id, s.get("id"), s.get("occ"),
                    int(bool(s.get("anyRight"))) if "anyRight" in s else None,
                    int(bool(s.get("hasSgRight"))) if "hasSgRight" in s else None,
                    int(bool(s.get("hasResRight"))) if "hasResRight" in s else None,
                    url,
                ),
            )

    def _save_my(self, snapshot_id: str, event_id: int, ts: str, url: str, data: Any):
        if not isinstance(data, list):
            return
        for seat_id in data:
            if isinstance(seat_id, int):
                self.conn.execute(
                    """INSERT OR REPLACE INTO my_seats
                       (snapshot_id,captured_at_utc,event_id,seat_id,source_url)
                       VALUES(?,?,?,?,?)""",
                    (snapshot_id, ts, event_id, seat_id, url),
                )

    def summary(self, snapshot_id: str, event_id: int):
        c = self.conn.cursor()

        total_seats = c.execute(
            "SELECT COUNT(*) FROM seats WHERE event_id=?", (event_id,)
        ).fetchone()[0]

        occ_rows = c.execute(
            """SELECT occ, COUNT(DISTINCT seat_id)
               FROM seat_occupancy
               WHERE snapshot_id=? AND event_id=?
               GROUP BY occ ORDER BY occ""",
            (snapshot_id, event_id),
        ).fetchall()

        my_count = c.execute(
            "SELECT COUNT(DISTINCT seat_id) FROM my_seats WHERE snapshot_id=? AND event_id=?",
            (snapshot_id, event_id),
        ).fetchone()[0]

        captured_endpoints = c.execute(
            """SELECT endpoint, COUNT(*) FROM raw_responses
               WHERE snapshot_id=? GROUP BY endpoint ORDER BY endpoint""",
            (snapshot_id,),
        ).fetchall()

        print("\n=== SNAPSHOT SUMMARY ===")
        print(f"event_id:          {event_id}")
        print(f"known seat map:    {total_seats:,}")
        print(f"my seats captured: {my_count}")
        print("occupancy records:")
        if occ_rows:
            for occ, n in occ_rows:
                print(f"  occ={occ}: {n:,} unique seats")
        else:
            print("  none captured")
        print("captured endpoints:")
        for ep, n in captured_endpoints:
            print(f"  {ep}: {n}")
        print(f"database:          {self.db_path}")
        print(f"raw responses:     {self.raw_dir}")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-id", type=int, required=True)
    parser.add_argument(
        "--url",
        help="Stadium URL. Defaults to https://bilety.legia.com/Stadium/Index?eventId=<id>",
    )
    parser.add_argument("--seconds", type=int, default=120,
                        help="How long to keep the browser open and capture responses.")
    parser.add_argument("--db", default="legia_inventory.sqlite")
    parser.add_argument("--raw-dir", default="raw")
    parser.add_argument(
        "--profile-dir",
        default=".browser-profile",
        help="Persistent Chromium profile. Keeps the same session between runs.",
    )
    args = parser.parse_args()

    event_id = args.event_id
    page_url = args.url or f"https://bilety.legia.com/Stadium/Index?eventId={event_id}"
    snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    store = Store(Path(args.db), Path(args.raw_dir))
    store.begin_snapshot(snapshot_id, event_id, page_url)

    seen_hashes: set[tuple[str, str]] = set()

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=args.profile_dir,
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )

        page = context.pages[0] if context.pages else await context.new_page()

        async def on_response(response: Response):
            url = response.url
            if not any(marker in url for marker in CAPTURE_MARKERS):
                return

            req_event_id = event_id_from_url(url)
            if req_event_id is not None and req_event_id != event_id:
                return

            try:
                raw = await response.text()
            except Exception as exc:
                print(f"[WARN] Cannot read {endpoint_name(url)}: {exc}")
                return

            digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
            key = (url, digest)
            if key in seen_hashes:
                return
            seen_hashes.add(key)

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[WARN] Non-JSON response: {url}")
                return

            store.save_response(snapshot_id, event_id, response, data, raw)

            extra = ""
            if isinstance(data, list):
                extra = f" ({len(data):,} records)"
            elif isinstance(data, dict) and "seats" in data and isinstance(data["seats"], list):
                extra = f" ({len(data['seats']):,} seats)"
            print(f"[CAPTURED] {endpoint_name(url)}{extra}")

        page.on("response", on_response)

        print("\nLegia Ticket Collector v0.1")
        print(f"Snapshot: {snapshot_id}")
        print(f"Event:    {event_id}")
        print(f"URL:      {page_url}")
        print("\nBrowser will remain open.")
        print("Navigate around the stadium / click sectors normally.")
        print("Every matching WGL response will be captured automatically.")
        print(f"Capture window: {args.seconds} seconds.\n")

        await page.goto(page_url, wait_until="domcontentloaded")
        await asyncio.sleep(args.seconds)

        store.summary(snapshot_id, event_id)
        await context.close()

if __name__ == "__main__":
    asyncio.run(main())
