#!/usr/bin/env python3

import asyncio
import json
import os
from urllib import request, error
from urllib.parse import urlparse

from playwright.async_api import async_playwright


EVENT_ID = int(os.environ["EVENT_ID"])
EVENT_URL = os.environ["ROBOTICKET_URL"]

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

ROBOTICKET_USERNAME = os.getenv("ROBOTICKET_USERNAME")
ROBOTICKET_PASSWORD = os.getenv("ROBOTICKET_PASSWORD")

SECTOR_INFO_ENDPOINT = "GetWGLSectorsInfo"


# ---------------------------------------------------------
# Supabase
# ---------------------------------------------------------

def api(path, method="GET", body=None, prefer=None):

    data = (
        None
        if body is None
        else json.dumps(body).encode("utf-8")
    )

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

        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

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


def parse_sector_inventory(data):

    sectors = []

    for sector in data.get("sectors", []):

        sector_id = sector.get("id")

        if sector_id is None:
            continue

        free_seats = 0

        for price_area in sector.get(
            "freeSeatsByPriceArea",
            [],
        ):
            free_seats += int(
                price_area.get(
                    "freeSeatsNo",
                    0,
                )
            )

        sectors.append(
            (
                str(sector_id),
                free_seats,
            )
        )

    return sectors


def insert_sector_inventory(snapshot_id, sectors):

    rows = [
        {
            "snapshot_id": snapshot_id,
            "event_id": EVENT_ID,
            "sector": sector_id,
            "available": available,
        }
        for sector_id, available in sectors
    ]

    api(
        "sector_inventory",
        "POST",
        rows,
    )

    return len(rows)


# ---------------------------------------------------------
# Optional Legia authentication
# ---------------------------------------------------------

async def login_if_required(page):

    if "konto.legia.com" not in page.url:
        print("Authentication not required.")
        return

    if not ROBOTICKET_USERNAME or not ROBOTICKET_PASSWORD:
        raise RuntimeError(
            "Authentication required but Roboticket credentials "
            "were not provided."
        )

    print("Authentication required.")

    email = page.locator(
        'input[formcontrolname="email"]:visible, '
        'input[type="email"]:visible'
    ).first

    password = page.locator(
        'input[formcontrolname="password"]:visible, '
        'input[type="password"]:visible'
    ).first

    await email.wait_for(
        state="visible",
        timeout=30000,
    )

    await password.wait_for(
        state="visible",
        timeout=30000,
    )

    await email.fill(
        ROBOTICKET_USERNAME
    )

    await password.fill(
        ROBOTICKET_PASSWORD
    )

    submit = page.get_by_role(
        "button",
        name="Zaloguj się",
        exact=False,
    ).first

    await submit.wait_for(
        state="visible",
        timeout=30000,
    )

    await page.wait_for_function(
        """
        () => {
            const button =
                Array.from(
                    document.querySelectorAll('button')
                )
                .find(
                    b =>
                        b.textContent
                        &&
                        b.textContent.includes('Zaloguj')
                );

            return button && !button.disabled;
        }
        """,
        timeout=30000,
    )

    print("Logging in...")

    await submit.click()

    await page.wait_for_url(
        lambda url:
            "bilety.legia.com" in url,
        timeout=30000,
    )

    print("Login successful:")
    print(page.url)


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

async def main():

    sector_data = None

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

        # -------------------------------------------------
        # Capture GetWGLSectorsInfo
        # -------------------------------------------------

        async def handle_response(response):

            nonlocal sector_data

            if SECTOR_INFO_ENDPOINT not in response.url:
                return

            try:
                data = await response.json()
            except Exception:
                return

            sector_data = data

            endpoint = (
                urlparse(response.url)
                .path
                .rsplit("/", 1)[-1]
            )

            print(
                f"Captured {response.status}: "
                f"{endpoint}"
            )

        page.on(
            "response",
            handle_response,
        )

        # -------------------------------------------------
        # Open event
        # -------------------------------------------------

        print("Opening event:")
        print(EVENT_URL)

        response = await page.goto(
            EVENT_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        print(
            "Initial status:",
            response.status
            if response
            else "none",
        )

        print(
            "Initial URL:",
            page.url,
        )

        await page.wait_for_timeout(2000)

        # -------------------------------------------------
        # Optional authentication
        # -------------------------------------------------

        await login_if_required(page)

        # If authentication redirected somewhere else,
        # return to exact requested event.
        if EVENT_URL not in page.url:

            print("Opening exact event page...")

            await page.goto(
                EVENT_URL,
                wait_until="domcontentloaded",
                timeout=90000,
            )

        print("Event page:")
        print(page.url)

        # -------------------------------------------------
        # Wait for Roboticket API
        # -------------------------------------------------

        print(
            "Waiting for GetWGLSectorsInfo..."
        )

        for _ in range(30):

            if sector_data is not None:
                break

            await page.wait_for_timeout(1000)

        if sector_data is None:

            raise RuntimeError(
                "GetWGLSectorsInfo was not captured."
            )

        # -------------------------------------------------
        # Parse
        # -------------------------------------------------

        sectors = parse_sector_inventory(
            sector_data
        )

        if not sectors:

            raise RuntimeError(
                "GetWGLSectorsInfo returned no sectors."
            )

        print("")
        print(
            f"Found {len(sectors)} sectors:"
        )

        for sector_id, available in sectors:

            print(
                f"Sector ID {sector_id}: "
                f"{available} available"
            )

        # -------------------------------------------------
        # Persist
        # -------------------------------------------------

        snapshot_id = create_snapshot()

        inserted = insert_sector_inventory(
            snapshot_id,
            sectors,
        )

        total_available = sum(
            available
            for _, available
            in sectors
        )

        print("")
        print(
            f"Created snapshot {snapshot_id}"
        )

        print(
            f"Inserted {inserted} sector records"
        )

        print(
            f"Total available seats: "
            f"{total_available}"
        )

        print("")
        print("SUCCESS")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
