#!/usr/bin/env python3

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib import error, parse, request
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright


ROBOTICKET_URL = "https://bilety.lechpoznan.pl/"
LECH_SCHEDULE_URL = "https://www.lechpoznan.pl/pierwsza-druzyna,61.html"
OUT = Path("roboticket_discovery_artifacts_v01")
MATRIX_PATH = OUT / "collector_matrix.json"
UNRESOLVED_PATH = OUT / "unresolved_events.json"
EVENT_ID_RE = re.compile(r"[?&]eventId=(\d+)", re.IGNORECASE)
RESERVATION_RE = re.compile(r"eventReservationSelector\((\d+)\)")
FIRST_TEAM_RE = re.compile(
    r"Kup bilet na wydarzenie(?: PREMIUM:)? Lech Poznań - (.+)",
    re.IGNORECASE,
)
WARSAW = ZoneInfo("Europe/Warsaw")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY", "")


def normalize_space(value):
    return re.sub(r"\s+", " ", value or "").strip()


def event_id_from_url(url):
    try:
        query = parse_qs(urlparse(url).query)
        values = query.get("eventId") or query.get("eventid")
        if values and values[0].isdigit():
            return values[0]
    except Exception:
        pass
    match = EVENT_ID_RE.search(url or "")
    return match.group(1) if match else None


def api_get_ticket_events():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    query = parse.urlencode(
        {
            "provider": "eq.roboticket",
            "select": "id,external_event_id,away_team,competition,match_date,kickoff_at",
            "limit": "1000",
        }
    )
    req = request.Request(
        f"{SUPABASE_URL}/rest/v1/ticket_events?{query}",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8") or "[]")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase {exc.code}: {detail}") from exc


def parse_schedule_fixture(item, opponent):
    text = normalize_space(item.get("text"))
    opponent_pattern = re.escape(opponent)
    pattern = re.compile(
        rf"(?P<day>\d{{2}})\s*\|\s*(?P<month>\d{{2}})\s*\|\s*(?P<year>\d{{4}})\s+"
        rf"(?P<hour>\d{{2}}):(?P<minute>\d{{2}})\s+Lech Poznań\s+-:-\s+{opponent_pattern}(?:\s|$)",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    kickoff = datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        hour,
        minute,
        tzinfo=WARSAW,
    )
    competition_icon = next(
        (
            image.get("src")
            for image in item.get("imgs", [])
            if "rth_0x21_" in (image.get("src") or "")
        ),
        None,
    )
    return {
        "match_date": kickoff.date().isoformat(),
        "kickoff_at": kickoff.isoformat(),
        "kickoff_placeholder": hour == 0 and minute == 0,
        "competition_icon": competition_icon,
        "schedule_text": text,
    }


async def discover_roboticket(page):
    response = await page.goto(
        ROBOTICKET_URL,
        wait_until="domcontentloaded",
        timeout=90000,
    )
    if not response or response.status >= 400:
        raise RuntimeError(
            f"Roboticket discovery failed: HTTP {response.status if response else 'none'}"
        )
    await page.wait_for_timeout(8000)

    boxes = await page.locator(".box").evaluate_all(
        r"""
        els => els.map(box => ({
            productid: box.getAttribute('productid') || '',
            controls: Array.from(box.querySelectorAll('a,button')).map(el => ({
                className: typeof el.className === 'string' ? el.className : '',
                href: el.getAttribute('href') || '',
                onclick: el.getAttribute('onclick') || '',
                aria: el.getAttribute('aria-label') || ''
            }))
        }))
        """
    )

    events = {}
    for box in boxes:
        for control in box.get("controls", []):
            if "btn-buy" not in (control.get("className") or ""):
                continue
            aria = normalize_space(control.get("aria"))
            match = FIRST_TEAM_RE.fullmatch(aria)
            if not match or "PREMIUM:" in aria.upper():
                continue

            opponent = normalize_space(match.group(1))
            event_id = event_id_from_url(control.get("href") or "")
            if not event_id:
                reservation = RESERVATION_RE.search(control.get("onclick") or "")
                if reservation:
                    event_id = reservation.group(1)
            if not event_id and str(box.get("productid", "")).isdigit():
                event_id = str(box["productid"])
            if not event_id:
                continue

            href = control.get("href") or ""
            event_url = (
                urljoin(ROBOTICKET_URL, href)
                if href
                else f"https://bilety.lechpoznan.pl/Stadium?eventId={event_id}"
            )
            events[event_id] = {
                "id": event_id,
                "provider": "roboticket",
                "home_team": "Lech Poznań",
                "away_team": opponent,
                "url": event_url,
            }

    return sorted(events.values(), key=lambda row: int(row["id"]))


async def load_schedule_items(page):
    response = await page.goto(
        LECH_SCHEDULE_URL,
        wait_until="domcontentloaded",
        timeout=90000,
    )
    if not response or response.status >= 400:
        raise RuntimeError(
            f"Official schedule discovery failed: HTTP {response.status if response else 'none'}"
        )
    await page.wait_for_timeout(4000)
    return await page.locator(".Item").evaluate_all(
        r"""
        els => els.map(el => ({
            text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim(),
            imgs: Array.from(el.querySelectorAll('img')).map(img => ({
                src: img.getAttribute('src') || '',
                alt: img.getAttribute('alt') || '',
                title: img.getAttribute('title') || ''
            }))
        }))
        """
    )


def match_schedule(events, schedule_items):
    resolved = []
    unresolved = []
    for event in events:
        matches = []
        for item in schedule_items:
            fixture = parse_schedule_fixture(item, event["away_team"])
            if fixture:
                matches.append(fixture)
        if len(matches) != 1:
            unresolved.append(
                {
                    **event,
                    "reason": "schedule_match_not_unique",
                    "schedule_match_count": len(matches),
                }
            )
            continue
        fixture = matches[0]
        if fixture["kickoff_placeholder"]:
            unresolved.append(
                {
                    **event,
                    **fixture,
                    "reason": "kickoff_is_placeholder_00_00",
                }
            )
            continue
        resolved.append({**event, **fixture})
    return resolved, unresolved


def apply_competition_mapping(resolved, existing_events):
    existing_by_id = {
        str(row.get("external_event_id")): row
        for row in existing_events
        if row.get("external_event_id") is not None
    }
    icon_to_competitions = {}

    for event in resolved:
        existing = existing_by_id.get(event["id"])
        competition = (existing or {}).get("competition")
        icon = event.get("competition_icon")
        if competition and icon:
            icon_to_competitions.setdefault(icon, set()).add(competition)

    icon_map = {
        icon: next(iter(values))
        for icon, values in icon_to_competitions.items()
        if len(values) == 1
    }

    matrix = []
    for event in resolved:
        competition = icon_map.get(event.get("competition_icon"))
        matrix.append(
            {
                "id": event["id"],
                "provider": "roboticket",
                "home_team": event["home_team"],
                "away_team": event["away_team"],
                "competition": competition or "",
                "match_date": event["match_date"],
                "kickoff_at": event["kickoff_at"],
                "mapping_source": "roboticket_homepage+lech_official_schedule_live",
                "mapping_confidence": "confirmed",
                "url": event["url"],
            }
        )
    return matrix, icon_map


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    existing_events = api_get_ticket_events()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1200},
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
        )
        roboticket_page = await context.new_page()
        schedule_page = await context.new_page()

        events = await discover_roboticket(roboticket_page)
        schedule_items = await load_schedule_items(schedule_page)
        resolved, unresolved = match_schedule(events, schedule_items)
        matrix, icon_map = apply_competition_mapping(resolved, existing_events)

        MATRIX_PATH.write_text(
            json.dumps(matrix, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        UNRESOLVED_PATH.write_text(
            json.dumps(unresolved, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (OUT / "competition_icon_map.json").write_text(
            json.dumps(icon_map, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (OUT / "resolved_events.json").write_text(
            json.dumps(resolved, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(f"Roboticket first-team normal events: {len(events)}")
        print(f"Resolved for collection: {len(matrix)}")
        print(f"Unresolved/skipped: {len(unresolved)}")
        for event in matrix:
            print(
                f"  {event['id']} | {event['away_team']} | "
                f"{event['kickoff_at']} | {event['competition'] or 'competition unresolved'}"
            )
        for event in unresolved:
            print(
                f"SKIP {event['id']} | {event['away_team']} | {event['reason']}"
            )
        print(f"MATRIX_JSON={MATRIX_PATH.read_text(encoding='utf-8')}")
        print("SUCCESS")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
