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


# -------------------------------------------------------------------
# SUPABASE
# -------------------------------------------------------------------

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
        raise RuntimeError(
            f"Supabase {exc.code}: {detail}"
        ) from exc


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


def upsert_seats(items):
    rows = []

    now = datetime.now(timezone.utc).isoformat()

    for seat in items:
        if not isinstance(seat, dict) or "id" not in seat:
            continue

        rows.append(
            {
                "event_id": EVENT_ID,
                "seat_id": seat["id"],
                "sector_id": seat.get("sectorId"),
                "row_label": seat.get("row"),
                "seat_label": seat.get("label"),
                "pa_id": seat.get("paId"),
                "x": seat.get("x"),
                "y": seat.get("y"),
                "angle": seat.get("a"),
                "last_seen_at": now,
            }
        )

    for i in range(0, len(rows), 500):
        api(
            "seats?on_conflict=event_id,seat_id",
            "POST",
            rows[i:i + 500],
            "resolution=merge-duplicates",
        )

    return len(rows)


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
        api(
            "seat_occupancy",
            "POST",
            rows[i:i + 500],
        )

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
        api(
            "my_seats",
            "POST",
            rows,
        )

    return len(rows)


# -------------------------------------------------------------------
# DIAGNOSTICS
# -------------------------------------------------------------------

async def screenshot(page, name):
    try:
        await page.screenshot(
            path=str(ART / name),
            full_page=True,
        )
    except Exception:
        pass


# -------------------------------------------------------------------
# ROBOTICKET LOGIN
# -------------------------------------------------------------------

async def login(page):

    print("Opening Roboticket home page...")

    await page.goto(
        HOME_URL,
        wait_until="domcontentloaded",
        timeout=90000,
    )

    await page.wait_for_timeout(3000)

    print("Current URL:", page.url)

    # ---------------------------------------------------------------
    # Open login form
    # ---------------------------------------------------------------

    login_candidates = [
        page.get_by_role(
            "link",
            name="Logowanie",
            exact=False,
        ),
        page.get_by_role(
            "button",
            name="Logowanie",
            exact=False,
        ),
        page.get_by_text(
            "Logowanie",
            exact=True,
        ),
    ]

    clicked = False

    for locator in login_candidates:
        try:
            count = await locator.count()

            if count == 0:
                continue

            for i in range(count):
                candidate = locator.nth(i)

                if await candidate.is_visible():
                    print("Opening login form...")
                    await candidate.click()
                    clicked = True
                    break

            if clicked:
                break

        except Exception:
            continue

    if clicked:
        await page.wait_for_timeout(2500)
    else:
        print(
            "Login button not clicked. "
            "Checking whether login form is already visible..."
        )

    await screenshot(
        page,
        "01-login-page.png",
    )

    # ---------------------------------------------------------------
    # Find ONLY visible login fields
    # ---------------------------------------------------------------

    email = page.locator(
        'input#Email:visible, '
        'input[type="email"]:visible, '
        'input[name*="mail" i]:visible, '
        'input[placeholder*="mail" i]:visible'
    ).first

    password = page.locator(
        'input[type="password"]:visible, '
        'input[name*="password" i]:visible, '
        'input[name*="haslo" i]:visible, '
        'input[placeholder*="has" i]:visible'
    ).first

    email_count = await email.count()
    password_count = await password.count()

    print("Visible email field:", email_count > 0)
    print("Visible password field:", password_count > 0)

    if not email_count or not password_count:

        await screenshot(
            page,
            "02-login-form-not-found.png",
        )

        (ART / "login-page.html").write_text(
            await page.content(),
            encoding="utf-8",
        )

        raise RuntimeError(
            "Visible Roboticket login form not found."
        )

    # ---------------------------------------------------------------
    # Fill credentials
    # ---------------------------------------------------------------

    print("Filling login credentials...")

    await email.fill(
        ROBOTICKET_USERNAME
    )

    await password.fill(
        ROBOTICKET_PASSWORD
    )

    # ---------------------------------------------------------------
    # Submit
    # ---------------------------------------------------------------

    submit_candidates = [
        page.get_by_role(
            "button",
            name="Zaloguj",
            exact=False,
        ),
        page.locator(
            'button[type="submit"]:visible'
        ),
        page.locator(
            'input[type="submit"]:visible'
        ),
    ]

    submitted = False

    for locator in submit_candidates:
        try:
            count = await locator.count()

            if count == 0:
                continue

            for i in range(count):
                candidate = locator.nth(i)

                if await candidate.is_visible():
                    print("Submitting login...")
                    await candidate.click()
                    submitted = True
                    break

            if submitted:
                break

        except Exception:
            continue

    if not submitted:

        await screenshot(
            page,
            "03-login-submit-not-found.png",
        )

        raise RuntimeError(
            "Visible Roboticket login submit control not found."
        )

    # Wait for authentication / redirect
    await page.wait_for_timeout(5000)

    print(
        "Login submitted. Current page:",
        page.url,
    )

    await screenshot(
        page,
        "04-after-login.png",
    )

    # ---------------------------------------------------------------
    # Basic failed-login detection
    # ---------------------------------------------------------------

    body_text = (
        await page.locator("body").inner_text()
    ).lower()

    failed_markers = (
        "nieprawidł",
        "nieprawidlow",
        "błędne hasło",
        "bledne haslo",
        "invalid password",
        "incorrect password",
    )

    if any(
        marker in body_text
        for marker in failed_markers
    ):

        await screenshot(
            page,
            "05-login-failed.png",
        )

        raise RuntimeError(
            "Roboticket rejected the login credentials."
        )

    print("Login step completed.")


# -------------------------------------------------------------------
# MAIN COLLECTOR
# -------------------------------------------------------------------

async def main():

    snapshot_id = create_snapshot()

    print(
        f"Created Supabase snapshot "
        f"{snapshot_id} for event {EVENT_ID}"
    )

    counts = {
        "seats": 0,
        "occ": 0,
        "my": 0,
    }

    captured_urls = set()
    console_lines = []

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

        # -----------------------------------------------------------
        # Browser diagnostics
        # -----------------------------------------------------------

        page.on(
            "console",
            lambda message: console_lines.append(
                f"{message.type}: {message.text}"
            ),
        )

        page.on(
            "pageerror",
            lambda exc: console_lines.append(
                f"PAGEERROR: {exc}"
            ),
        )

        # -----------------------------------------------------------
        # Capture Roboticket WGL responses
        # -----------------------------------------------------------

        async def handle(response):

            url = response.url

            if not any(
                marker in url
                for marker in CAPTURE
            ):
                return

            if url in captured_urls:
                return

            try:
                data = await response.json()
            except Exception:
                return

            captured_urls.add(url)

            endpoint = (
                urlparse(url)
                .path
                .rsplit("/", 1)[-1]
            )

            print(
                f"Captured {response.status}: "
                f"{endpoint}"
            )

            # -------------------------------------------------------
            # Seat definitions
            # -------------------------------------------------------

            if endpoint == "GetWGLSeats":

                items = (
                    data.get("seats", [])
                    if isinstance(data, dict)
                    else []
                )

                count = upsert_seats(items)

                counts["seats"] += count

                print(
                    f"  seat definitions: {len(items)}"
                )

            # -------------------------------------------------------
            # Occupancy
            # -------------------------------------------------------

            elif (
                endpoint == "GetWGLSeatsOccInfo"
                and isinstance(data, list)
            ):

                count = insert_occ(
                    snapshot_id,
                    data,
                )

                counts["occ"] += count

                print(
                    f"  occupancy records: {len(data)}"
                )

            # -------------------------------------------------------
            # Seats associated with current session
            # -------------------------------------------------------

            elif (
                endpoint == "GetWGLSeatsMyInfo"
                and isinstance(data, list)
            ):

                count = insert_my(
                    snapshot_id,
                    data,
                )

                counts["my"] += count

                print(
                    f"  session seats: {len(data)}"
                )

        page.on(
            "response",
            handle,
        )

        # -----------------------------------------------------------
        # LOGIN
        # -----------------------------------------------------------

        await login(page)

        # -----------------------------------------------------------
        # OPEN EVENT
        # -----------------------------------------------------------

        print(
            f"Opening event {EVENT_ID}..."
        )

        response = await page.goto(
            EVENT_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        print(
            "Event HTTP status:",
            response.status
            if response
            else "none",
        )

        print(
            "Event final URL:",
            page.url,
        )

        print(
            "Event title:",
            await page.title(),
        )

        # Roboticket loads the stadium asynchronously.
        await page.wait_for_timeout(20000)

        await screenshot(
            page,
            "06-event-page.png",
        )

        # Additional time for WGL XHR requests.
        await page.wait_for_timeout(10000)

        # -----------------------------------------------------------
        # Save diagnostics
        # -----------------------------------------------------------

        (ART / "console.txt").write_text(
            "\n".join(console_lines),
            encoding="utf-8",
        )

        (ART / "event-page.html").write_text(
            await page.content(),
            encoding="utf-8",
        )

        print("DONE")

        print(
            json.dumps(
                counts,
                indent=2,
            )
        )

        await browser.close()

    # ---------------------------------------------------------------
    # Validate result
    # ---------------------------------------------------------------

    if counts["seats"] == 0:

        raise RuntimeError(
            "Login completed but no WGL seat data was captured. "
            "Download the roboticket-diagnostics artifact "
            "from this workflow run."
        )


if __name__ == "__main__":
    asyncio.run(main())
