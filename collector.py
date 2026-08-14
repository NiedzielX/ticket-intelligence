#!/usr/bin/env python3

import asyncio
import json
import os
import re
from pathlib import Path
from urllib import request, error

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
            "sector": sector,
            "available": available,
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
# Collector
# ---------------------------------------------------------

async def main():

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
        # 1. Open protected event
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
        # 2. Login
        # -------------------------------------------------

        if "konto.legia.com" in page.url:

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

            print("Waiting for login form...")

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
        # 3. Ensure exact event page
        # -------------------------------------------------

        if (
            "bilety.legia.com" not in page.url
            or f"eventId={EVENT_ID}" not in page.url
        ):

            print("Opening exact event page...")

            await page.goto(
                EVENT_URL,
                wait_until="domcontentloaded",
                timeout=90000,
            )

        print("Event page loaded:")
        print(page.url)

        # -------------------------------------------------
        # 4. WAIT FOR ACTUAL STADIUM INVENTORY
        # -------------------------------------------------

        print(
            "Waiting for Roboticket "
            "sector inventory..."
        )

        try:

            await page.wait_for_function(
                """
                () => {
                    const text =
                        document.body
                        ? document.body.innerText
                        : '';

                    return (
                        text.includes('Miejsc wolnych')
                        &&
                        text.includes('Sektor')
                    );
                }
                """,
                timeout=60000,
            )

            print(
                "Sector inventory loaded."
            )

        except Exception:

            print(
                "Sector inventory did not appear "
                "within 60 seconds."
            )

            await page.screenshot(
                path=str(
                    ART / "no-sector-inventory.png"
                ),
                full_page=True,
            )

            (ART / "no-sector-inventory.html").write_text(
                await page.content(),
                encoding="utf-8",
            )

            body_debug = await page.locator(
                "body"
            ).inner_text()

            (ART / "no-sector-inventory.txt").write_text(
                body_debug,
                encoding="utf-8",
            )

            raise RuntimeError(
                "Roboticket event loaded, "
                "but sector inventory did not appear."
            )

        # Give Angular a small moment to finish the list
        await page.wait_for_timeout(3000)

        # -------------------------------------------------
        # 5. Extract sector inventory
        # -------------------------------------------------

        body_text = await page.locator(
            "body"
        ).inner_text()

        pattern = re.compile(
            r"Sektor\s+(\d+)"
            r"[\s\S]{0,100}?"
            r"Miejsc\s+wolnych\s*(\d+)",
            re.IGNORECASE,
        )

        matches = pattern.findall(
            body_text
        )

        sectors = []

        seen = set()

        for sector, available in matches:

            key = (
                sector,
                int(available),
            )

            if key not in seen:

                seen.add(key)

                sectors.append(key)

        print("")
        print(
            f"Found {len(sectors)} sectors:"
        )

        for sector, available in sectors:

            print(
                f"Sector {sector}: "
                f"{available} available"
            )

        if not sectors:

            await page.screenshot(
                path=str(
                    ART / "sector-regex-failed.png"
                ),
                full_page=True,
            )

            (ART / "sector-page-text.txt").write_text(
                body_text,
                encoding="utf-8",
            )

            raise RuntimeError(
                "Sector inventory appeared, "
                "but no sector values could be parsed."
            )

        # -------------------------------------------------
        # 6. Store snapshot
        # -------------------------------------------------

        snapshot_id = create_snapshot()

        print("")
        print(
            f"Created snapshot {snapshot_id}"
        )

        inserted = insert_sector_inventory(
            snapshot_id,
            sectors,
        )

        total_available = sum(
            available
            for _, available in sectors
        )

        print(
            f"Inserted {inserted} sectors"
        )

        print(
            f"Total visible availability: "
            f"{total_available}"
        )

        print("")
        print("SUCCESS")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
