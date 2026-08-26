# Cloudflare emergency Roboticket collector

Temporary raw-data fallback while GitHub Actions hosted runners are unavailable.

## What it does

- runs as a Cloudflare Worker;
- uses Cloudflare Browser Run with `@cloudflare/playwright`;
- reads future canonical Lech home events from Supabase;
- opens the Roboticket event page in a real browser session;
- calls `GetWGLSectorsInfo` from inside the same page context so Roboticket receives browser cookies, client-side state and same-origin request context;
- writes canonical `snapshots` and `sector_inventory` rows directly to Supabase;
- freezes `event_match_date_at_capture` and `event_kickoff_at_capture`;
- processes all active events sequentially in one browser instance;
- does not run features, forecasts, evaluation or calibration.

The Worker is configured for hourly collection at minute 13. This cadence is intentional because Browser Run on Workers Free includes 10 browser minutes per day.

## Required secrets

Configure these as Cloudflare Worker secrets. Do not commit their values:

- `SUPABASE_SECRET_KEY`
- `MANUAL_RUN_TOKEN` (only needed for `/run`)

`SUPABASE_URL`, `EVENT_PROVIDER` and `EVENT_HOME_TEAM` are non-secret Wrangler variables.

## Manual validation

After deployment, invoke `/run` with:

```text
Authorization: Bearer <MANUAL_RUN_TOKEN>
```

A successful log contains entries similar to:

```text
Browser page ready for 10069: status=200, finalUrl=...
Roboticket browser sector response for 10069: status=200, sectors=...
```

and a JSON result with a new `snapshot_id` and `available_total`.

If Browser Run still receives an empty body from `GetWGLSectorsInfo`, treat that as evidence that Roboticket is filtering Cloudflare/browser-automation traffic rather than merely requiring cookies/session bootstrap.

Do not merge this emergency collector to `main` until a live Browser Run invocation confirms that Roboticket returns sector JSON and Supabase writes succeed.
