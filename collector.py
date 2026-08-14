#!/usr/bin/env python3

import asyncio
import json
import os
from pathlib import Path
from urllib import request, error
from urllib.parse import urlparse

from playwright.async_api import async_playwright


EVENT_ID = int(os.getenv("EVENT_ID", "8009"))
EVENT_URL = f"https://bilety.legia.com/Stadium/Index?eventId={EVENT_ID}"

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
)


# ------------------------------------------------------------
# Supabase
# ------------------------------------------------------------

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


def insert_occ(snapshot_id, items):
    rows = []

    for seat in items:
        if not isinstance(seat, dict):
            continue

        if "id" not in seat:
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


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

async def main():

    counts = {
        "occ": 0,
        "my": 0,
    }

    snapshot_id = None

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

        # --------------------------------------------------------
        # General browser diagnostics
        # --------------------------------------------------------

        console_lines = []

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

        # --------------------------------------------------------
        # Capture Roboticket inventory API
        # --------------------------------------------------------

        async def handle_inventory_response(response):
            nonlocal snapshot_id

            url = response.url

            if not any(
                marker in url
                for marker in CAPTURE
            ):
                return

            try:
                data = await response.json()
            except Exception:
                return

            endpoint = (
                urlparse(url)
                .path
                .rsplit("/", 1)[-1]
            )

            print(
                f"Captured {response.status}: {endpoint}"
            )

            if snapshot_id is None:
                snapshot_id = create_snapshot()

                print(
                    f"Created Supabase snapshot "
                    f"{snapshot_id} for event {EVENT_ID}"
                )

            if (
                endpoint == "GetWGLSeatsOccInfo"
                and isinstance(data, list)
            ):
                count = insert_occ(
                    snapshot_id,
                    data,
                )

                counts["occ"] += count

                print(
                    f"Occupancy records: {len(data)}"
                )

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
                    f"My session seats: {len(data)}"
                )

        page.on(
            "response",
            handle_inventory_response,
        )

        # --------------------------------------------------------
        # Diagnostic listener for auth endpoint
        # --------------------------------------------------------

        async def log_auth_response(response):

            if "konto.legia.com/login" not in response.url:
                return

            print("")
            print("====================================")
            print("AUTH RESPONSE")
            print("====================================")

            print(
                "Status:",
                response.status,
            )

            print(
                "URL:",
                response.url,
            )

            try:
                body = await response.text()

                print(
                    "Response preview:"
                )

                print(
                    body[:3000]
                )

                (ART / "auth-response.txt").write_text(
                    body,
                    encoding="utf-8",
                )

            except Exception as exc:
                print(
                    "Cannot read auth response:",
                    exc,
                )

            print(
                "===================================="
            )
            print("")

        page.on(
            "response",
            log_auth_response,
        )

        # --------------------------------------------------------
        # 1. Open protected event
        # --------------------------------------------------------

        print(
            "Opening protected event:"
        )

        print(
            EVENT_URL
        )

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

        await page.wait_for_timeout(
            2000
        )

        # --------------------------------------------------------
        # 2. Authentication
        # --------------------------------------------------------

        if "konto.legia.com" in page.url:

            print(
                "Authentication page detected."
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

            print(
                "Filling credentials..."
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

            print(
                "Waiting for Angular "
                "to enable login button..."
            )

            await page.wait_for_function(
                """
                () => {
                    const buttons =
                        Array.from(
                            document.querySelectorAll('button')
                        );

                    const button =
                        buttons.find(
                            b =>
                                b.textContent
                                && b.textContent.includes('Zaloguj')
                        );

                    return button && !button.disabled;
                }
                """,
                timeout=30000,
            )

            print(
                "Login form valid. Submitting..."
            )

            # ----------------------------------------------------
            # Submit auth
            # ----------------------------------------------------

            await submit.click()

            await page.wait_for_timeout(
                5000
            )

            print("")
            print(
                "URL after submit:",
                page.url,
            )

            # ----------------------------------------------------
            # Dump page text after login attempt
            # ----------------------------------------------------

            body_text = await page.locator(
                "body"
            ).inner_text()

            print("")
            print(
                "===================================="
            )

            print(
                "PAGE TEXT AFTER LOGIN:"
            )

            print(
                "===================================="
            )

            print(
                body_text[:5000]
            )

            print(
                "===================================="
            )

            print("")

            # ----------------------------------------------------
            # Save post-login diagnostics
            # ----------------------------------------------------

            await page.screenshot(
                path=str(
                    ART / "after-login.png"
                ),
                full_page=True,
            )

            (ART / "after-login.html").write_text(
                await page.content(),
                encoding="utf-8",
            )

            (ART / "after-login-text.txt").write_text(
                body_text,
                encoding="utf-8",
            )

            # ----------------------------------------------------
            # Wait for normal SSO redirect
            # ----------------------------------------------------

            print(
                "Waiting for SSO redirect "
                "back to Roboticket..."
            )

            try:
                await page.wait_for_url(
                    lambda url:
                        "bilety.legia.com" in url,
                    timeout=30000,
                )

            except Exception:
                print(
                    "No automatic redirect "
                    "within 30s."
                )

            print(
                "URL after authentication:",
                page.url,
            )

        # --------------------------------------------------------
        # 3. Ensure exact event page
        # --------------------------------------------------------

        if (
            "bilety.legia.com" not in page.url
            or f"eventId={EVENT_ID}" not in page.url
        ):

            print(
                "Opening exact event "
                "after authentication..."
            )

            await page.goto(
                EVENT_URL,
                wait_until="domcontentloaded",
                timeout=90000,
            )

        print(
            "Final event URL:",
            page.url,
        )

        # --------------------------------------------------------
        # 4. Wait for Roboticket XHR
        # --------------------------------------------------------

        await page.wait_for_timeout(
            30000
        )

        # --------------------------------------------------------
        # Final diagnostics
        # --------------------------------------------------------

        try:
            await page.screenshot(
                path=str(
                    ART / "final-page.png"
                ),
                full_page=True,
            )
        except Exception:
            pass

        try:
            (ART / "final-page.html").write_text(
                await page.content(),
                encoding="utf-8",
            )
        except Exception:
            pass

        (ART / "console.txt").write_text(
            "\n".join(console_lines),
            encoding="utf-8",
        )

        print("")
        print("DONE")
        print(
            json.dumps(
                counts,
                indent=2,
            )
        )

        await browser.close()

    # ------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------

    if counts["occ"] == 0:
        raise RuntimeError(
            "Authentication finished but no occupancy "
            "data was captured. "
            "Check AUTH RESPONSE and PAGE TEXT AFTER LOGIN."
        )


if __name__ == "__main__":
    asyncio.run(main())
