#!/usr/bin/env python3

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import async_playwright


START_URL = "https://bilety.lechpoznan.pl/"
OUT = Path("roboticket_discovery_artifacts_v01")
EVENT_ID_RE = re.compile(r"[?&]eventId=(\d+)", re.IGNORECASE)


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


def add_unique(target, value):
    value = (value or "").strip()
    if value and value not in target:
        target.append(value)


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
                {
                    "status": response.status,
                    "url": url,
                    "content_type": content_type,
                }
            )
            if "json" not in content_type:
                return
            try:
                payload = await response.json()
            except Exception:
                return
            if json_contains_event_marker(payload):
                captured_json.append(
                    {
                        "status": response.status,
                        "url": url,
                        "payload": payload,
                    }
                )

        page.on("response", handle_response)

        print(f"Opening {START_URL}")
        response = await page.goto(
            START_URL,
            wait_until="domcontentloaded",
            timeout=90000,
        )
        print("Initial status:", response.status if response else "none")
        await page.wait_for_timeout(8000)

        anchors = await page.locator("a[href]").evaluate_all(
            r"""
            els => els.map(a => {
                const ancestors = [];
                let node = a;
                for (let depth = 0; depth < 7 && node; depth += 1, node = node.parentElement) {
                    const text = (node.innerText || node.textContent || '')
                        .replace(/\s+/g, ' ')
                        .trim();
                    ancestors.push({
                        depth,
                        tag: node.tagName || '',
                        className: typeof node.className === 'string' ? node.className : '',
                        text: text.slice(0, 1200),
                        html: (node.outerHTML || '').slice(0, 6000)
                    });
                }
                return {
                    href: a.getAttribute('href') || '',
                    text: (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim(),
                    aria: a.getAttribute('aria-label') || '',
                    title: a.getAttribute('title') || '',
                    ancestors
                };
            })
            """
        )

        dom_events = {}
        for anchor in anchors:
            absolute = urljoin(page.url, anchor["href"])
            event_id = event_id_from_url(absolute)
            if not event_id:
                continue
            current = dom_events.setdefault(
                event_id,
                {
                    "event_id": event_id,
                    "urls": [],
                    "texts": [],
                    "ancestor_texts": [],
                    "anchor_contexts": [],
                },
            )
            add_unique(current["urls"], absolute)
            for value in (anchor.get("text"), anchor.get("aria"), anchor.get("title")):
                add_unique(current["texts"], value)

            context_record = {
                "href": absolute,
                "text": anchor.get("text"),
                "aria": anchor.get("aria"),
                "title": anchor.get("title"),
                "ancestors": anchor.get("ancestors", []),
            }
            current["anchor_contexts"].append(context_record)
            for ancestor in anchor.get("ancestors", []):
                text = (ancestor.get("text") or "").strip()
                if text and len(text) <= 1200:
                    add_unique(current["ancestor_texts"], text)

        body_text = await page.locator("body").inner_text()
        page_info = {
            "final_url": page.url,
            "title": await page.title(),
            "body_excerpt": body_text[:12000],
            "anchor_count": len(anchors),
        }

        ordered_events = sorted(dom_events.values(), key=lambda x: int(x["event_id"]))
        (OUT / "dom_events.json").write_text(
            json.dumps(ordered_events, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (OUT / "captured_event_json.json").write_text(
            json.dumps(captured_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (OUT / "response_index.json").write_text(
            json.dumps(response_index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (OUT / "page_info.json").write_text(
            json.dumps(page_info, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(f"DOM event ids: {[row['event_id'] for row in ordered_events]}")
        for row in ordered_events:
            candidate_contexts = [
                text for text in row["ancestor_texts"]
                if len(text) > 20 and text not in row["texts"]
            ]
            best = candidate_contexts[0] if candidate_contexts else " | ".join(row["texts"])
            print(f"Event {row['event_id']}: {best[:500]}")
        print(f"Candidate JSON responses: {len(captured_json)}")
        print(f"Indexed responses: {len(response_index)}")
        print("SUCCESS")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
