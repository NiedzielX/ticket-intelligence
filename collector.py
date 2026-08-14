#!/usr/bin/env python3

import asyncio
import json
import os
from pathlib import Path
from urllib import request, error
from urllib.parse import urlparse

from playwright.async_api import async_playwright


EVENT_ID = int(os.getenv("EVENT_ID", "8009"))

EVENT_URL = (
    f"https://bilety.legia.com/Stadium/Index"
    f"?eventId={EVENT_ID}"
)

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

ROBOTICKET_USERNAME = os.environ["ROBOTICKET_USERNAME"]
ROBOTICKET_PASSWORD = os.environ["ROBOTICKET_PASSWORD"]

ART = Path("artifacts")
ART.mkdir(exist_ok=True)

SECTOR_ENDPOINTS = (
    "GetWGLSectorsInfo",
    "GetWGLSectorsInfoExt",
)


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


def insert_sector_inventory(snapshot_id, sectors):

    rows = [
        {
            "snapshot_id": snapshot_id,
            "event_id": EVENT_ID,
            "sector": str(sector),
            "available": int(available),
        }
        for sector, available in sectors
    ]

    api(
        "sector_inventory",
        "POST",
        rows,
    )

    return len(rows)


# ---------------------------------------------------------
# Generic Roboticket sector parser
# ---------------------------------------------------------

SECTOR_KEYS = (
    "sector",
    "sectorId",
    "sectorID",
    "id",
    "label",
    "name",
    "sectorName",
)

AVAILABLE_KEYS = (
    "available",
    "availableCount",
    "free",
    "freeCount",
    "freeSeats",
    "availableSeats",
    "placesLeft",
    "seatsLeft",
    "vacant",
    "vacantCount",
    "count",
)


def extract_sector_records(data):

    found = []

    def walk(value):

        if isinstance(value, list):

            for item in value:
                walk(item)

            return

        if not isinstance(value, dict):
            return

        sector = None
        available = None

        for key in SECTOR_KEYS:

            if key in value:
                candidate = value[key]

                if isinstance(candidate, (str, int)):
                    sector = candidate
                    break

        for key in AVAILABLE_KEYS:

            if key in value:
                candidate = value[key]

                if isinstance(candidate, (int, float)):
                    available = int(candidate)
                    break

                if (
                    isinstance(candidate, str)
                    and candidate.isdigit()
                ):
                    available = int(candidate)
                    break

        if (
            sector is not None
            and available is not None
        ):
            found.append(
                (
                    str(sector),
                    available,
                )
            )

        for child in value.values():

            if isinstance(child, (dict, list)):
                walk(child)

    walk(data)

    result = []
    seen = set()

    for sector, available in found:

        key = (
            sector,
            available,
        )

        if key not in seen:
            seen.add(key)
            result.append(key)

    return result


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

async def main():

    captured_sector_json = []
    parsed_sectors = []

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
        # Capture sector API responses
        # -------------------------------------------------

        async def handle_response(response):

            nonlocal parsed_sectors

            url = response.url

            if not any(
                endpoint in url
                for endpoint in SECTOR_ENDPOINTS
            ):
                return

            endpoint = (
                urlparse(url)
                .path
                .rsplit("/", 1)[-1]
            )

            print("")
            print(
                f"Captured sector endpoint: "
                f"{response.status} {endpoint}"
            )

            try:
                data = await response.json()

            except Exception as exc:

                print(
                    "Could not decode JSON:",
                    exc,
                )

                return

            captured_sector_json.append(
                {
                    "endpoint": endpoint,
                    "url": url,
                    "data": data,
                }
            )

            safe_name = (
                endpoint
                .replace("/", "_")
            )

            (
                ART
                / f"{safe_name}.json"
            ).write_text(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            candidates = extract_sector_records(
                data
            )

            print(
                f"Parsed candidate records: "
                f"{len(candidates)}"
            )

            for item in candidates[:50]:
                print(item)

            for item in candidates:

                if item not in parsed_sectors:
                    parsed_sectors.append(item)

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
            response.status if response else "none",
        )

        print(
            "Initial URL:",
            page.url,
        )

        await page.wait_for_timeout(2000)

        # -------------------------------------------------
        # Login
        # -------------------------------------------------

        if "konto.legia.com" in page.url:

            print(
                "Authentication required."
            )

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

        # -------------------------------------------------
        # Ensure event
        # -------------------------------------------------

        if (
            "bilety.legia.com" not in page.url
            or f"eventId={EVENT_ID}" not in page.url
        ):

            await page.goto(
                EVENT_URL,
                wait_until="domcontentloaded",
                timeout=90000,
            )

        print("Event page:")
        print(page.url)

        # -------------------------------------------------
        # Let Roboticket call its APIs
        # -------------------------------------------------

        print(
            "Waiting for Roboticket sector API..."
        )

        for seconds in range(1, 31):

            if captured_sector_json:
                break

            await page.wait_for_timeout(1000)

            if seconds % 5 == 0:
                print(
                    f"Waiting... {seconds}s"
                )

        print("")

        print(
            "Sector API responses captured:",
            len(captured_sector_json),
        )

        # -------------------------------------------------
        # Fallback diagnostics
        # -------------------------------------------------

        if not captured_sector_json:

            print(
                "No GetWGLSectorsInfo response "
                "captured."
            )

            await page.screenshot(
                path=str(
                    ART / "sector-api-not-called.png"
                ),
                full_page=True,
            )

            (
                ART / "event-page.html"
            ).write_text(
                await page.content(),
                encoding="utf-8",
            )

            raise RuntimeError(
                "Roboticket did not call "
                "GetWGLSectorsInfo during this run."
            )

        # -------------------------------------------------
        # If generic parser already succeeded
        # -------------------------------------------------

        if parsed_sectors:

            print(
                f"Parsed {len(parsed_sectors)} "
                f"sector inventory records."
            )

            snapshot_id = create_snapshot()

            inserted = insert_sector_inventory(
                snapshot_id,
                parsed_sectors,
            )

            total = sum(
                available
                for _, available
                in parsed_sectors
            )

            print(
                f"Created snapshot {snapshot_id}"
            )

            print(
                f"Inserted {inserted} sectors"
            )

            print(
                f"Total parsed availability: {total}"
            )

            print("SUCCESS")

        else:

            print("")
            print(
                "Sector endpoint captured, "
                "but field names are not yet mapped."
            )

            print(
                "Raw JSON has been saved "
                "to roboticket-diagnostics."
            )

            # IMPORTANT:
            # Do not mark the workflow as failed here.
            # Authentication and API capture worked.
            print(
                "DIAGNOSTIC SUCCESS"
            )

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
