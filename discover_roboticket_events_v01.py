#!/usr/bin/env python3

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import async_playwright


ROBOTICKET_URL = "https://bilety.lechpoznan.pl/"
LECH_SCHEDULE_URL = "https://www.lechpoznan.pl/pierwsza-druzyna,61.html"
OUT = Path("roboticket_discovery_artifacts_v01")
EVENT_ID_RE = re.compile(r"[?&]eventId=(\d+)", re.IGNORECASE)
RESERVATION_RE = re.compile(r"eventReservationSelector\((\d+)\)")
FIRST_TEAM_RE = re.compile(
    r"Kup bilet na wydarzenie(?: PREMIUM:)? Lech Poznań - (.+)",
    re.IGNORECASE,
)


def event_id_from_url(url):
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        values = query.get("eventId") or query.get("eventid")
        if values and values[0].isdigit():
            return values[0]
    except Exception:
        pass
    match = EVENT_ID_RE.search(url or "")
    return match.group(1) if match else None


def json_contains_event_marker(value):
    if isinstance(value, dict):
        for key, item in value.items():
            key_l = str(key).lower()
            if key_l in {"eventid", "event_id", "event", "events"}:
                return True
            if json_contains_event_marker(item):
                return True
    elif isinstance(value, list):
        return any(json_contains_event_marker(item) for item in value[:100])
    elif isinstance(value, str):
        return "eventid=" in value.lower() or "/stadium" in value.lower()
    return False


def normalize_space(value):
    return re.sub(r"\s+", " ", value or "").strip()


def schedule_snippet(body_text, opponent, radius=700):
    pos = body_text.lower().find(opponent.lower())
    if pos < 0:
        return None
    start = max(0, pos - radius)
    end = min(len(body_text), pos + len(opponent) + radius)
    return body_text[start:end]


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    captured_json = []
    response_index = []

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
        page = await context.new_page()

        async def handle_response(response):
            content_type = (response.headers.get("content-type") or "").lower()
            url = response.url
            if "json" not in content_type and not any(
                token in url.lower() for token in ("event", "stadium", "sale", "offer")
            ):
                return
            response_index.append(
                {"status": response.status, "url": url, "content_type": content_type}
            )
            if "json" not in content_type:
                return
            try:
                payload = await response.json()
            except Exception:
                return
            if json_contains_event_marker(payload):
                captured_json.append(
                    {"status": response.status, "url": url, "payload": payload}
                )

        page.on("response", handle_response)

        print(f"Opening Roboticket: {ROBOTICKET_URL}")
        response = await page.goto(
            ROBOTICKET_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )
        print("Roboticket status:", response.status if response else "none")
        await page.wait_for_timeout(8000)

        boxes = await page.locator(".box").evaluate_all(
            r"""
            els => els.map(box => ({
                productid: box.getAttribute('productid') || '',
                productlistid: box.getAttribute('productlistid') || '',
                producttype: box.getAttribute('producttype') || '',
                bannertype: box.getAttribute('bannertype') || '',
                html: (box.outerHTML || '').slice(0, 9000),
                controls: Array.from(box.querySelectorAll('a,button')).map(el => ({
                    tag: el.tagName || '',
                    className: typeof el.className === 'string' ? el.className : '',
                    href: el.getAttribute('href') || '',
                    onclick: el.getAttribute('onclick') || '',
                    aria: el.getAttribute('aria-label') || '',
                    title: el.getAttribute('title') || '',
                    text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()
                }))
            }))
            """
        )

        discovered = []
        normal_first_team = []
        for box in boxes:
            buy_controls = [
                c for c in box.get("controls", [])
                if "btn-buy" in (c.get("className") or "")
                and "Kup bilet na wydarzenie" in (c.get("aria") or "")
            ]
            for control in buy_controls:
                aria = normalize_space(control.get("aria"))
                match = FIRST_TEAM_RE.fullmatch(aria)
                is_first_team = bool(match)
                opponent = normalize_space(match.group(1)) if match else None
                is_premium = "PREMIUM:" in aria.upper()

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
                if href:
                    event_url = urljoin(ROBOTICKET_URL, href)
                else:
                    event_url = f"https://bilety.lechpoznan.pl/Stadium?eventId={event_id}"

                row = {
                    "event_id": event_id,
                    "event_url": event_url,
                    "aria": aria,
                    "opponent": opponent,
                    "is_first_team": is_first_team,
                    "is_premium": is_premium,
                    "discovery_mode": "href" if href else "eventReservationSelector",
                    "productid": box.get("productid"),
                    "productlistid": box.get("productlistid"),
                    "producttype": box.get("producttype"),
                    "bannertype": box.get("bannertype"),
                }
                discovered.append(row)
                if is_first_team and not is_premium:
                    normal_first_team.append(row)

        # Dedupe by provider event id.
        normal_by_id = {row["event_id"]: row for row in normal_first_team}
        normal_first_team = sorted(normal_by_id.values(), key=lambda x: int(x["event_id"]))
        discovered = sorted(discovered, key=lambda x: int(x["event_id"]))

        roboticket_body = await page.locator("body").inner_text()
        (OUT / "roboticket_boxes.json").write_text(
            json.dumps(boxes, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (OUT / "discovered_buy_events.json").write_text(
            json.dumps(discovered, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (OUT / "normal_first_team_events.json").write_text(
            json.dumps(normal_first_team, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (OUT / "captured_event_json.json").write_text(
            json.dumps(captured_json, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (OUT / "response_index.json").write_text(
            json.dumps(response_index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (OUT / "roboticket_body.txt").write_text(roboticket_body, encoding="utf-8")

        print("Normal first-team Roboticket events:")
        for row in normal_first_team:
            print(
                f"  {row['event_id']} | {row['opponent']} | "
                f"{row['discovery_mode']} | {row['event_url']}"
            )

        schedule_page = await context.new_page()
        print(f"Opening official Lech schedule: {LECH_SCHEDULE_URL}")
        schedule_response = await schedule_page.goto(
            LECH_SCHEDULE_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )
        print("Schedule status:", schedule_response.status if schedule_response else "none")
        await schedule_page.wait_for_timeout(4000)
        schedule_body = await schedule_page.locator("body").inner_text()
        (OUT / "official_lech_schedule_body.txt").write_text(
            schedule_body,
            encoding="utf-8",
        )

        snippets = {}
        for row in normal_first_team:
            opponent = row.get("opponent")
            if not opponent:
                continue
            snippet = schedule_snippet(schedule_body, opponent)
            snippets[row["event_id"]] = {
                "opponent": opponent,
                "found": snippet is not None,
                "snippet": snippet,
            }
            print(f"Official schedule context for {row['event_id']} / {opponent}:")
            print(normalize_space(snippet)[:1000] if snippet else "  NOT FOUND")

        (OUT / "official_schedule_snippets.json").write_text(
            json.dumps(snippets, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(f"Candidate JSON responses: {len(captured_json)}")
        print(f"Indexed responses: {len(response_index)}")
        print("SUCCESS")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
