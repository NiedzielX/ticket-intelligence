#!/usr/bin/env python3

import asyncio
import json
import os
import re
from datetime import datetime
from urllib import request, error
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright


SOURCE_URL = "https://www.frekwencyjny.pl/kluby/lech-poznan"

CLUB = "Lech Poznań"

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]


MATCH_PATTERN = re.compile(
    r"Lech\s+"
    r"(\d+):(\d+)\s+"
    r"(.+?)\s+"
    r"([0-9][0-9 \u00a0]*)\s+"
    r"(\d{4}/\d{4})\s*"
    r"•\s*"
    r"([^•]+?)\s*"
    r"•\s*"
    r"(\d{2}\.\d{2}\.\d{4})\s+"
    r"(\d{2}:\d{2})",
    re.MULTILINE,
)


# ---------------------------------------------------------
# Supabase
# ---------------------------------------------------------

def api(path, method="GET", body=None, prefer=None):

    data = (
        None
        if body is None
        else json.dumps(
            body,
            ensure_ascii=False,
        ).encode("utf-8")
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
        with request.urlopen(
            req,
            timeout=30,
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            return (
                json.loads(raw)
                if raw
                else None
            )

    except error.HTTPError as exc:

        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Supabase {exc.code}: {detail}"
        ) from exc


# ---------------------------------------------------------
# Parser
# ---------------------------------------------------------

def parse_matches(text):

    matches = []

    for match in MATCH_PATTERN.finditer(text):

        (
            home_goals,
            away_goals,
            opponent,
            attendance,
            season,
            competition,
            date,
            time,
        ) = match.groups()

        attendance = int(
            attendance
            .replace(" ", "")
            .replace("\u00a0", "")
        )

        local_dt = datetime.strptime(
            f"{date} {time}",
            "%d.%m.%Y %H:%M",
        ).replace(
            tzinfo=ZoneInfo("Europe/Warsaw")
        )

        matches.append(
            {
                "club": CLUB,
                "opponent": opponent.strip(),
                "home_goals": int(home_goals),
                "away_goals": int(away_goals),
                "attendance": attendance,
                "season": season.strip(),
                "competition": competition.strip(),
                "match_date": local_dt.isoformat(),
                "source": "frekwencyjny.pl",
                "source_url": SOURCE_URL,
            }
        )

    return matches


# ---------------------------------------------------------
# Supabase insert
# ---------------------------------------------------------

def upsert_matches(matches):

    if not matches:
        return

    for i in range(
        0,
        len(matches),
        100,
    ):

        batch = matches[i:i + 100]

        api(
            (
                "historical_matches?"
                "on_conflict=club,opponent,"
                "match_date,source"
            ),
            "POST",
            batch,
            "resolution=merge-duplicates",
        )


# ---------------------------------------------------------
# Main scraper
# ---------------------------------------------------------

async def main():

    all_matches = {}

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        page = await browser.new_page(
            viewport={
                "width": 1440,
                "height": 1200,
            }
        )

        print("Opening:")
        print(SOURCE_URL)

        await page.goto(
            SOURCE_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        await page.wait_for_timeout(2000)

        page_number = 1

        while True:

            print("")
            print(
                f"Reading page {page_number}..."
            )

            text = await page.locator(
                "body"
            ).inner_text()

            page_matches = parse_matches(
                text
            )

            print(
                f"Found {len(page_matches)} matches"
            )

            for match in page_matches:

                key = (
                    match["opponent"],
                    match["match_date"],
                )

                all_matches[key] = match

            next_button = page.get_by_role(
                "button",
                name=re.compile(
                    "następna",
                    re.IGNORECASE,
                ),
            )

            if await next_button.count() == 0:
                print(
                    "Next button not found."
                )
                break

            next_button = next_button.first

            if await next_button.is_disabled():
                print(
                    "Last page reached."
                )
                break

            old_text = text

            await next_button.click()

            # Wait until pagination actually changes.
            try:

                await page.wait_for_function(
                    """
                    previous => {
                        return (
                            document.body.innerText
                            !== previous
                        );
                    }
                    """,
                    old_text,
                    timeout=10000,
                )

            except Exception:
                await page.wait_for_timeout(
                    1500
                )

            page_number += 1

            if page_number > 30:
                raise RuntimeError(
                    "Pagination safety limit reached."
                )

        await browser.close()

    records = list(
        all_matches.values()
    )

    records.sort(
        key=lambda x: x["match_date"]
    )

    print("")
    print(
        f"TOTAL UNIQUE MATCHES: "
        f"{len(records)}"
    )

    if len(records) < 200:

        raise RuntimeError(
            "Unexpectedly low number of matches. "
            "Import aborted."
        )

    print(
        "First:",
        records[0]["match_date"],
        records[0]["opponent"],
        records[0]["attendance"],
    )

    print(
        "Last:",
        records[-1]["match_date"],
        records[-1]["opponent"],
        records[-1]["attendance"],
    )

    print("")
    print(
        "Uploading to Supabase..."
    )

    upsert_matches(
        records
    )

    print(
        f"SUCCESS: {len(records)} "
        f"historical matches imported."
    )


if __name__ == "__main__":
    asyncio.run(main())
