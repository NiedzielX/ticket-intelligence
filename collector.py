#!/usr/bin/env python3

import asyncio
import json
import os
from pathlib import Path
from urllib import request, error
from urllib.parse import urlparse

from playwright.async_api import async_playwright

EVENT_ID = int(os.getenv("EVENT_ID", "8009"))
EVENT_URL = f"https://bilety.legia.com/Stadium/Index?eventId={EVENT_ID}"

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

ROBOTICKET_USERNAME = os.environ["ROBOTICKET_USERNAME"]
ROBOTICKET_PASSWORD = os.environ["ROBOTICKET_PASSWORD"]

ART = Path("artifacts")
ART.mkdir(exist_ok=True)

CAPTURE = (
    "GetWGLSeats?",
    "GetWGLSeatsOccInfo?",
    "GetWGLSeatsMyInfo?",
)

def api(path, method="GET", body=None, prefer=None):
    data = None if body is None else json.dumps(body).encode("utf-8")

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
        with request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase {exc.code}: {detail}") from exc

def create_snapshot():
    result = api(
        "snapshots",
        "POST",
        {
            "event_id": EVENT_ID,
            "source": "roboticket",
        },
        "return=representation",
    )
    return result[0]["id"]

def insert_occ(snapshot_id, items):
    rows = []

    for seat in items:
        if not isinstance(seat, dict) or "id" not in seat:
            continue

        rows.append(
            {
                "snapshot_id": snapshot_id,
                "event_id": EVENT_ID,
                "seat_id": seat["id"],
                "occ": seat.get("occ"),
                "any_right": seat.get("anyRight"),
                "has_sg_right": seat.get("hasSgRight"),
                "has_res_right": seat.get("hasResRight"),
            }
        )

    for i in range(0, len(rows), 500):
        api("seat_occupancy", "POST", rows[i:i+500])

    return len(rows)

def insert_my(snapshot_id, items):
    rows = [
        {
            "snapshot_id": snapshot_id,
            "event_id": EVENT_ID,
            "seat_id": seat_id,
        }
        for seat_id in items
        if isinstance(seat_id, int)
    ]

    if rows:
        api("my_seats", "POST", rows)

    return len(rows)

async def main():
    snapshot_id = create_snapshot()

    print(f"Created Supabase snapshot {snapshot_id} for event {EVENT_ID}")

    counts = {
        "occ": 0,
        "my": 0,
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        context = await browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000,
            },
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
        )

        page = await context.new_page()

        async def handle(response):
            url = response.url

            if not any(marker in url for marker in CAPTURE):
                return

            try:
                data = await response.json()
            except Exception:
                return

            endpoint = urlparse(url).path.rsplit("/", 1)[-1]

            print(f"Captured {response.status}: {endpoint}")

            if endpoint == "GetWGLSeatsOccInfo" and isinstance(data, list):
                counts["occ"] += insert_occ(
                    snapshot_id,
                    data,
                )

            elif endpoint == "GetWGLSeatsMyInfo" and isinstance(data, list):
                counts["my"] += insert_my(
                    snapshot_id,
                    data,
                )

        page.on("response", handle)

        print("Opening event directly:")
        print(EVENT_URL)

        response = await page.goto(
            EVENT_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        print("Initial URL:", page.url)
        print("HTTP status:", response.status if response else "none")

        await page.wait_for_timeout(3000)

        # If redirected to login/auth page, fill visible login form
        email = page.locator(
            'input#Email:visible, '
            'input[name="Email"]:visible, '
            'input[type="email"]:visible'
        ).first

        password = page.locator(
            'input[type="password"]:visible'
        ).first

        if await email.count() and await password.count():
            print("Login form detected.")

            await email.fill(ROBOTICKET_USERNAME)
            await password.fill(ROBOTICKET_PASSWORD)

            submit = page.locator(
                'button[type="submit"]:visible, '
                'input[type="submit"]:visible'
            ).first

            if not await submit.count():
                raise RuntimeError("Login form found but submit button not found.")

            print("Submitting login...")
            await submit.click()

            await page.wait_for_timeout(5000)

            print("After login URL:", page.url)

            # Make sure we return to the exact event
            if f"eventId={EVENT_ID}" not in page.url:
                print("Returning to event page...")
                await page.goto(
                    EVENT_URL,
                    wait_until="domcontentloaded",
                    timeout=90000,
                )

        else:
            print("No visible login form detected. Continuing on event page.")

        print("Final event URL:", page.url)

        await page.wait_for_timeout(30000)

        await page.screenshot(
            path=str(ART / "event-page.png"),
            full_page=True,
        )

        (ART / "event-page.html").write_text(
            await page.content(),
            encoding="utf-8",
        )

        print("DONE")
        print(json.dumps(counts, indent=2))

        await browser.close()

    if counts["occ"] == 0:
        raise RuntimeError(
            "No occupancy data captured. "
            "Download roboticket-diagnostics from this run."
        )

if __name__ == "__main__":
    asyncio.run(main())
