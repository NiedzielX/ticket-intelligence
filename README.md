# Legia Ticket Inventory Collector v0.1

Experimental collector for the public Roboticket stadium view used by Legia.

## What we already confirmed from the captured responses

For event `8009`:

- `GetWGLSeats` returns the stadium seat definition (`seat_id`, `sectorId`, row, seat label, coordinates).
- The supplied response contained **31,213 seat definitions**.
- `GetWGLSeatsOccInfo` returns dynamic per-seat records with fields such as `id`, `occ`, `anyRight`, `hasSgRight`, `hasResRight`.
- `GetWGLSeatsMyInfo` returns seat IDs associated with the current browser session/cart.
- We experimentally observed `GetWGLSeatsMyInfo` change from:
  - `[354647, 354685, 354727]`
  - to `[354685, 354727]`
  after removing one seat.

We **do not yet assign a business meaning to `occ=0` and `occ=2`**. The collector records raw states first; interpretation comes after repeated controlled observations.

## Why Playwright

Roboticket appends a dynamic `vaoKeysForCache` query parameter to some XHR calls and uses browser/session state.

Instead of trying to imitate those calls immediately, v0.1 opens a normal Chromium session and listens to the same responses that the public website receives.

This gives us a reliable dataset first. We can optimise the collector later.

## Install

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Windows activation:

```powershell
.venv\Scripts\activate
```

## Capture a snapshot

```bash
python collector.py --event-id 8009 --seconds 120
```

The browser opens.

During the 120-second capture window:

1. let the stadium page load;
2. click through the sectors you want to observe;
3. do not buy anything;
4. the collector records matching WGL responses automatically.

Outputs:

- `legia_inventory.sqlite` — normalized database;
- `raw/` — every unique captured JSON response;
- `.browser-profile/` — persistent browser session.

## Repeat

Run the same command later:

```bash
python collector.py --event-id 8009 --seconds 120
```

Each run gets a new `snapshot_id`.

For a real future experiment, run it repeatedly from the beginning of public sale until kickoff.

## Compare snapshots

```bash
python analyze.py --event-id 8009
```

It shows:

- snapshots,
- number of occupancy records,
- `occ` distribution,
- state transitions between the two latest snapshots,
- first changed seats mapped to sector / row / seat where possible.

## Database model

### `seats`
Static-ish seat map:

- `event_id`
- `seat_id`
- `sector_id`
- `row_label`
- `seat_label`
- `pa_id`
- coordinates

### `seat_occupancy`
Dynamic observations:

- `snapshot_id`
- `captured_at_utc`
- `event_id`
- `seat_id`
- `occ`
- rights flags
- original source URL

### `my_seats`
Seat IDs belonging to the collector's own browser session/cart.

### `raw_responses`
Raw JSON plus request URL and SHA-256. Keep this table: it lets us reinterpret fields later without losing source data.

## Important analytical rule

Do **not** call a disappearance from public inventory a sale yet.

A seat can change because of:

- cart hold,
- release,
- reservation,
- allocation change,
- season-ticket logic,
- other Roboticket state.

The first objective is to learn the state machine empirically:

- `0 -> 2`
- `2 -> 0`
- appearing/disappearing records
- duration of each state
- relationship with `MyInfo`

Only then should we define a derived `likely_sold` event.

## v0.2

Next useful version:

1. discover and record sector metadata / names;
2. automatically traverse all selectable sectors;
3. ensure one complete stadium snapshot per run;
4. calculate inventory by sector;
5. classify temporary holds vs persistent removals;
6. scheduler: T-30/T-14/T-7/T-1 cadence;
7. add public match metadata and final attendance.
