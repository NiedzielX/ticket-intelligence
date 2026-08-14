#!/usr/bin/env python3
import asyncio
import json
import os
from datetime import datetime, timezone
from urllib import request, error
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright

EVENT_ID = int(os.getenv("EVENT_ID", "8009"))
PAGE_URL = os.getenv(
    "ROBOTICKET_URL",
    f"https://bilety.legia.com/Stadium/Index?eventId={EVENT_ID}",
)
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

CAPTURE = (
    "GetWGLSeats?",
    "GetWGLSeatsOccInfo?",
    "GetWGLSeatsMyInfo?",
)

def api(path, method="GET", body=None, prefer=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    req = request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else None
    except error.HTTPError as e:
        detail = e.read().decode()
        raise RuntimeError(f"Supabase {e.code}: {detail}") from e

def create_snapshot():
    result = api(
        "snapshots",
        "POST",
        {"event_id": EVENT_ID, "source": "roboticket"},
        "return=representation",
    )
    return result[0]["id"]

def upsert_seats(items):
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for s in items:
        if not isinstance(s, dict) or "id" not in s:
            continue
        rows.append({
            "event_id": EVENT_ID,
            "seat_id": s["id"],
            "sector_id": s.get("sectorId"),
            "row_label": s.get("row"),
            "seat_label": s.get("label"),
            "pa_id": s.get("paId"),
            "x": s.get("x"),
            "y": s.get("y"),
            "angle": s.get("a"),
            "last_seen_at": now,
        })
    for i in range(0, len(rows), 500):
        api(
            "seats?on_conflict=event_id,seat_id",
            "POST",
            rows[i:i+500],
            "resolution=merge-duplicates",
        )
    return len(rows)

def insert_occ(snapshot_id, items):
    rows = []
    for s in items:
        if not isinstance(s, dict) or "id" not in s:
            continue
        rows.append({
            "snapshot_id": snapshot_id,
            "event_id": EVENT_ID,
            "seat_id": s["id"],
            "occ": s.get("occ"),
            "any_right": s.get("anyRight"),
            "has_sg_right": s.get("hasSgRight"),
            "has_res_right": s.get("hasResRight"),
        })
    for i in range(0, len(rows), 500):
        api("seat_occupancy", "POST", rows[i:i+500])
    return len(rows)

def insert_my(snapshot_id, items):
    rows = [
        {"snapshot_id": snapshot_id, "event_id": EVENT_ID, "seat_id": x}
        for x in items if isinstance(x, int)
    ]
    if rows:
        api("my_seats", "POST", rows)
    return len(rows)

async def main():
    snapshot_id = create_snapshot()
    print(f"Created Supabase snapshot {snapshot_id} for event {EVENT_ID}")

    counts = {"seats": 0, "occ": 0, "my": 0}
    processed = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})

        async def handle(response):
            url = response.url
            if not any(x in url for x in CAPTURE):
                return
            if url in processed:
                return

            try:
                data = await response.json()
            except Exception:
                return

            processed.add(url)
            endpoint = urlparse(url).path.rsplit("/", 1)[-1]

            if endpoint == "GetWGLSeats":
                items = data.get("seats", []) if isinstance(data, dict) else []
                counts["seats"] += upsert_seats(items)
                print(f"Captured {len(items)} seat definitions")

            elif endpoint == "GetWGLSeatsOccInfo" and isinstance(data, list):
                counts["occ"] += insert_occ(snapshot_id, data)
                print(f"Captured {len(data)} occupancy records")

            elif endpoint == "GetWGLSeatsMyInfo" and isinstance(data, list):
                counts["my"] += insert_my(snapshot_id, data)
                print(f"Captured {len(data)} current-session seats")

        page.on("response", handle)

        print(f"Opening {PAGE_URL}")
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(15000)

        # v0.2 best-effort sector traversal:
        # click visible elements whose text is a 3-digit sector number.
        # This deliberately avoids checkout / purchase actions.
        sector_labels = page.locator("text=/^[1-3][0-9]{2}$/")
        n = min(await sector_labels.count(), 80)
        print(f"Visible sector-like labels: {n}")

        for i in range(n):
            try:
                el = sector_labels.nth(i)
                if not await el.is_visible():
                    continue
                await el.click(timeout=2500)
                await page.wait_for_timeout(1800)
                # If click navigated to a seat-level view, go back.
                if "Stadium" in page.url:
                    await page.go_back(wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(1000)
            except Exception:
                continue

        await page.wait_for_timeout(5000)
        await browser.close()

    print("DONE")
    print(json.dumps(counts))
    if counts["seats"] == 0:
        raise RuntimeError("No Roboticket seat data captured. Workflow did not reach the WGL seat API.")

if __name__ == "__main__":
    asyncio.run(main())
