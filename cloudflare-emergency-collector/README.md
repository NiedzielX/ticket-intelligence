# Cloudflare emergency collector

Temporary raw Roboticket snapshot collector used while GitHub Actions hosted-runner minutes are exhausted.

## What it does

- Runs from a Cloudflare Cron Trigger configured for the Worker.
- Loads future canonical Lech home events from `ticket_events` in Supabase.
- Calls Roboticket `GetWGLSectorsInfo` directly over HTTP for each active event.
- Sums `freeSeatsNo` across `freeSeatsByPriceArea` for every returned sector.
- Writes one canonical `snapshots` row and matching `sector_inventory` rows per event.
- Freezes `event_match_date_at_capture` and `event_kickoff_at_capture` from the canonical event at collection time.
- Does **not** calculate live features, forecasts, outcomes or calibration. Those can be replayed later from the raw snapshots.

The collector does not use Cloudflare Browser Rendering or Playwright.

## Required runtime configuration

Runtime variables:

- `SUPABASE_URL`
- `EVENT_PROVIDER` (default `roboticket`)
- `EVENT_HOME_TEAM` (default `Lech Poznań`)

Secrets:

- `SUPABASE_SECRET_KEY`
- `MANUAL_RUN_TOKEN` only if `/run` should be used manually

Do not commit secret values to GitHub or `wrangler.jsonc`.

Generate a manual-run token locally, for example:

```bash
openssl rand -hex 24
```

## Test

After deploy, the Worker has a public health endpoint:

```text
/health
```

For an authenticated manual collection run:

```bash
curl -H "Authorization: Bearer <MANUAL_RUN_TOKEN>" https://<worker-url>/run
```

The normal collector is triggered automatically by the Cloudflare Cron Trigger.

## Collection semantics

`available` is the number reported by Roboticket as `freeSeatsNo` for the sector, summed across all price areas. It is an inventory/demand proxy, not confirmed ticket sales.

## After GitHub Actions resets

1. Keep the Cloudflare collector running until GitHub collection is confirmed healthy again.
2. Rebuild live features / forecast observations from the Cloudflare-collected raw snapshots where needed.
3. Disable the Cloudflare Cron Trigger to avoid duplicate snapshots.
4. Do not delete the raw snapshots already collected.

This emergency collector intentionally does not replace canonical event discovery. It only collects events already present in `ticket_events`.
