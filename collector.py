#!/usr/bin/env python3

import asyncio
import json
import os
from datetime import datetime, timezone
from urllib import request, error
from urllib.parse import urlencode, urlparse

from playwright.async_api import async_playwright


EVENT_ID = int(os.environ["EVENT_ID"])
EVENT_URL = os.environ["ROBOTICKET_URL"]
EVENT_PROVIDER = os.getenv("EVENT_PROVIDER", "roboticket")
EVENT_HOME_TEAM = os.getenv("EVENT_HOME_TEAM")
EVENT_AWAY_TEAM = os.getenv("EVENT_AWAY_TEAM")
EVENT_COMPETITION = os.getenv("EVENT_COMPETITION")
EVENT_MATCH_DATE = os.getenv("EVENT_MATCH_DATE")
EVENT_KICKOFF_AT = os.getenv("EVENT_KICKOFF_AT")
EVENT_MAPPING_SOURCE = os.getenv("EVENT_MAPPING_SOURCE")
EVENT_MAPPING_CONFIDENCE = os.getenv("EVENT_MAPPING_CONFIDENCE", "high")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

ROBOTICKET_USERNAME = os.getenv("ROBOTICKET_USERNAME")
ROBOTICKET_PASSWORD = os.getenv("ROBOTICKET_PASSWORD")

SECTOR_INFO_ENDPOINT = "GetWGLSectorsInfo"


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


def resolve_ticket_event_id():
    query = urlencode(
        {
            "provider": f"eq.{EVENT_PROVIDER}",
            "external_event_id": f"eq.{EVENT_ID}",
            "select": "id",
        }
    )
    result = api(f"ticket_events?{query}")
    if not result:
        raise RuntimeError(
            f"No ticket_events mapping found for {EVENT_PROVIDER}:{EVENT_ID}."
        )
    if len(result) != 1:
        raise RuntimeError(
            f"Expected one ticket_events mapping for {EVENT_PROVIDER}:{EVENT_ID}, "
            f"found {len(result)}."
        )
    return result[0]["id"]


def sync_ticket_event_metadata(ticket_event_id):
    required = {
        "EVENT_HOME_TEAM": EVENT_HOME_TEAM,
        "EVENT_AWAY_TEAM": EVENT_AWAY_TEAM,
        "EVENT_MATCH_DATE": EVENT_MATCH_DATE,
        "EVENT_KICKOFF_AT": EVENT_KICKOFF_AT,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing event metadata configuration: " + ", ".join(missing)
        )

    result = api(
        f"ticket_events?id=eq.{ticket_event_id}",
        "PATCH",
        {
            "home_team": EVENT_HOME_TEAM,
            "away_team": EVENT_AWAY_TEAM,
            "competition": EVENT_COMPETITION,
            "match_date": EVENT_MATCH_DATE,
            "kickoff_at": EVENT_KICKOFF_AT,
            "source_url": EVENT_URL,
            "mapping_source": EVENT_MAPPING_SOURCE,
            "mapping_confidence": EVENT_MAPPING_CONFIDENCE,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        "return=representation",
    )
    if not result or len(result) != 1:
        raise RuntimeError(f"Failed to update ticket event {ticket_event_id}.")
    return result[0]


def create_snapshot(ticket_event_id):
    result = api(
        "snapshots",
        "POST",
        {
            "event_id": EVENT_ID,
            "source": EVENT_PROVIDER,
            "ticket_event_id": ticket_event_id,
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
        free_seats = sum(
            int(price_area.get("freeSeatsNo", 0))
            for price_area in sector.get("freeSeatsByPriceArea", [])
        )
        sectors.append((str(sector_id), free_seats))
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
    api("sector_inventory", "POST", rows)
    return len(rows)


async def login_if_required(page):
    if "konto.legia.com" not in page.url:
        print("Authentication not required.")
        return
    if not ROBOTICKET_USERNAME or not ROBOTICKET_PASSWORD:
        raise RuntimeError(
            "Authentication required but Roboticket credentials were not provided."
        )

    print("Authentication required.")
    email = page.locator(
        'input[formcontrolname="email"]:visible, input[type="email"]:visible'
    ).first
    password = page.locator(
        'input[formcontrolname="password"]:visible, input[type="password"]:visible'
    ).first
    await email.wait_for(state="visible", timeout=30000)
    await password.wait_for(state="visible", timeout=30000)
    await email.fill(ROBOTICKET_USERNAME)
    await password.fill(ROBOTICKET_PASSWORD)

    submit = page.get_by_role("button", name="Zaloguj się", exact=False).first
    await submit.wait_for(state="visible", timeout=30000)
    await page.wait_for_function(
        """
        () => {
            const button = Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent && b.textContent.includes('Zaloguj'));
            return button && !button.disabled;
        }
        """,
        timeout=30000,
    )
    print("Logging in...")
    await submit.click()
    await page.wait_for_url(lambda url: "bilety.legia.com" in url, timeout=30000)
    print("Login successful:")
    print(page.url)


async def main():
    sector_data = None

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

        async def handle_response(response):
            nonlocal sector_data
            if SECTOR_INFO_ENDPOINT not in response.url:
                return
            try:
                sector_data = await response.json()
            except Exception:
                return
            endpoint = urlparse(response.url).path.rsplit("/", 1)[-1]
            print(f"Captured {response.status}: {endpoint}")

        page.on("response", handle_response)

        print("Opening event:")
        print(EVENT_URL)
        response = await page.goto(
            EVENT_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )
        print("Initial status:", response.status if response else "none")
        print("Initial URL:", page.url)
        await page.wait_for_timeout(2000)

        await login_if_required(page)

        if EVENT_URL not in page.url:
            print("Opening exact event page...")
            await page.goto(
                EVENT_URL,
                wait_until="domcontentloaded",
                timeout=90000,
            )

        print("Event page:")
        print(page.url)
        print("Waiting for GetWGLSectorsInfo...")

        for _ in range(30):
            if sector_data is not None:
                break
            await page.wait_for_timeout(1000)

        if sector_data is None:
            raise RuntimeError("GetWGLSectorsInfo was not captured.")

        sectors = parse_sector_inventory(sector_data)
        if not sectors:
            raise RuntimeError("GetWGLSectorsInfo returned no sectors.")

        print("")
        print(f"Found {len(sectors)} sectors:")
        for sector_id, available in sectors:
            print(f"Sector ID {sector_id}: {available} available")

        ticket_event_id = resolve_ticket_event_id()
        event_metadata = sync_ticket_event_metadata(ticket_event_id)

        print(
            f"Resolved ticket event {ticket_event_id} "
            f"for {EVENT_PROVIDER}:{EVENT_ID}"
        )
        print("Event kickoff:", event_metadata["kickoff_at"])

        snapshot_id = create_snapshot(ticket_event_id)
        inserted = insert_sector_inventory(snapshot_id, sectors)
        total_available = sum(available for _, available in sectors)

        print("")
        print(f"Created snapshot {snapshot_id}")
        print(f"Inserted {inserted} sector records")
        print(f"Total available seats: {total_available}")
        print("")
        print("SUCCESS")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
