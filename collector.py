#!/usr/bin/env python3
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error
from urllib.parse import urlparse

from playwright.async_api import async_playwright

EVENT_ID = int(os.getenv("EVENT_ID", "8009"))
HOME_URL = "https://bilety.legia.com/"
EVENT_URL = os.getenv(
    "ROBOTICKET_URL",
    f"https://bilety.legia.com/Stadium/Index?eventId={EVENT_ID}",
)

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
    "GetWGLSectorsInfo?",
    "GetWGLSectorsInfoExt?",
    "GetWGLSeatsInfo?",
    "GetWGLSeatsInfoExt?",
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
        with request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
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

async def screenshot(page, name):
    try:
        await page.screenshot(path=str(ART / name), full_page=True)
    except Exception:
        pass

async def login(page):
    print("Opening Roboticket home page...")
    await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(3000)

    # Try to open the normal supporter login UI.
    login_candidates = [
        page.get_by_role("link", name="Logowanie"),
        page.get_by_role("button", name="Logowanie"),
        page.get_by_text("Logowanie", exact=True),
    ]

    clicked = False
    for locator in login_candidates:
        try:
            if await locator.count() and await locator.first.is_visible():
                await locator.first.click()
                clicked = True
                break
        except Exception:
            continue

    if clicked:
        await page.wait_for_timeout(2500)

    # Robust selectors: first prefer semantic/placeholder matches,
    # then standard HTML email/password input types.
    email = page.locator(
        'input[type="email"], '
        'input[name*="mail" i], '
        'input[placeholder*="mail" i]'
    ).first
    password = page.locator(
        'input[type="password"], '
        'input[name*="password" i], '
        'input[name*="haslo" i], '
        'input[placeholder*="has" i]'
    ).first

    if not await email.count() or not await password.count():
        await screenshot(page, "login-form-not-found.png")
        (ART / "login-page.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError("Roboticket login form not found.")

    await email.fill(ROBOTICKET_USERNAME)
    await password.fill(ROBOTICKET_PASSWORD)

    submit_candidates = [
        page.get_by_role("button", name="Zaloguj"),
        page.get_by_role("button", name="Zaloguj się"),
        page.locator('button[type="submit"]'),
        page.locator('input[type="submit"]'),
    ]

    submitted = False
    for locator in submit_candidates:
        try:
            if await locator.count() and await locator.first.is_visible():
                await locator.first.click()
                submitted = True
                break
        except Exception:
            continue

    if not submitted:
        await screenshot(page, "login-submit-not-found.png")
        raise RuntimeError("Roboticket login submit control not found.")

    await page.wait_for_timeout(5000)

    # We deliberately don't log cookies, tokens or account data.
    # A visible password input after submit is a strong signal login failed.
    visible_password = False
    try:
        visible_password = await password.is_visible()
    except Exception:
        pass

    body_text = (await page.locator("body").inner_text()).lower()

    if visible_password and (
        "nieprawid" in body_text
        or "błęd" in body_text
        or "invalid" in body_text
        or "incorrect" in body_text
    ):
        await screenshot(page, "login-failed.png")
        raise RuntimeError("Roboticket rejected the login credentials.")

    print("Login submitted. Current page:", page.url)

async def main():
    snapshot_id = create_snapshot()
    print(f"Created Supabase snapshot {snapshot_id} for event {EVENT_ID}")

    counts = {"seats": 0, "occ": 0, "my": 0}
    captured_urls = set()
    console_lines = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )

        context = await browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
        )
        page = await context.new_page()

        page.on("console", lambda m: console_lines.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: console_lines.append(f"PAGEERROR: {e}"))

        async def handle(response):
            url = response.url
            if not any(marker in url for marker in CAPTURE):
                return

            # Avoid writing the identical response URL more than once
            # during this single run.
            if url in captured_urls:
                return

            try:
                data = await response.json()
            except Exception:
                return

            captured_urls.add(url)
            endpoint = urlparse(url).path.rsplit("/", 1)[-1]
            print(f"Captured {response.status}: {endpoint}")

            if endpoint == "GetWGLSeats":
                items = data.get("seats", []) if isinstance(data, dict) else []
                counts["seats"] += upsert_seats(items)
                print(f"  seat definitions: {len(items)}")

            elif endpoint == "GetWGLSeatsOccInfo" and isinstance(data, list):
                counts["occ"] += insert_occ(snapshot_id, data)
                print(f"  occupancy records: {len(data)}")

            elif endpoint == "GetWGLSeatsMyInfo" and isinstance(data, list):
                counts["my"] += insert_my(snapshot_id, data)
                print(f"  session seats: {len(data)}")

        page.on("response", handle)

        await login(page)

        print(f"Opening event {EVENT_ID}...")
        response = await page.goto(
            EVENT_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )
        print("Event HTTP status:", response.status if response else "none")
        print("Event final URL:", page.url)
        print("Event title:", await page.title())

        await page.wait_for_timeout(20000)
        await screenshot(page, "event-page.png")

        # Give JS more time to request stadium/map data.
        await page.wait_for_timeout(10000)

        (ART / "console.txt").write_text(
            "\n".join(console_lines),
            encoding="utf-8",
        )
        (ART / "event-page.html").write_text(
            await page.content(),
            encoding="utf-8",
        )

        print("DONE")
        print(json.dumps(counts))

        await browser.close()

    if counts["seats"] == 0:
        raise RuntimeError(
            "Login completed but no WGL seat data was captured. "
            "Download the roboticket-diagnostics artifact from this run."
        )

if __name__ == "__main__":
    asyncio.run(main())
