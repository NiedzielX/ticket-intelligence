# Cloudflare emergency collector

Temporary raw Roboticket snapshot collector used while GitHub Actions hosted-runner minutes are exhausted.

## What it does

- Runs every 2 hours at minute 13 UTC via Cloudflare Cron Trigger.
- Loads future canonical Lech home events from `ticket_events` in Supabase.
- Uses one Cloudflare Browser Run / Playwright browser session.
- Opens each event sequentially.
- Captures `GetWGLSectorsInfo`.
- Writes one canonical `snapshots` row and matching `sector_inventory` rows.
- Freezes `event_match_date_at_capture` and `event_kickoff_at_capture` from the canonical event at collection time.
- Does **not** calculate live features, forecasts, outcomes or calibration. Those can be replayed later from the raw snapshots.

## One-time deployment

From this directory:

```bash
npm install
npx wrangler login
npx wrangler secret put SUPABASE_URL
npx wrangler secret put SUPABASE_SECRET_KEY
npx wrangler secret put ROBOTICKET_USERNAME
npx wrangler secret put ROBOTICKET_PASSWORD
npx wrangler secret put MANUAL_RUN_TOKEN
npm run deploy
```

Do not commit secret values to GitHub or `wrangler.jsonc`.

`ROBOTICKET_USERNAME` and `ROBOTICKET_PASSWORD` are only used if Roboticket redirects the browser to the existing login flow.

Generate a manual-run token locally, for example:

```bash
openssl rand -hex 24
```

Then store that output with:

```bash
npx wrangler secret put MANUAL_RUN_TOKEN
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

The normal collector is triggered automatically by the configured Cron Trigger.

## Why 2 hours

The forecast evaluation currently allows a maximum early horizon gap of 2.5 hours. A two-hour snapshot cadence therefore preserves enough raw observations to select a valid strict pre-horizon snapshot for T-14, T-7, T-72h, T-48h and T-24h, assuming the collector is healthy.

## After GitHub Actions resets

1. Keep the Cloudflare collector running until GitHub collection is confirmed healthy again.
2. Rebuild live features / forecast observations from the Cloudflare-collected raw snapshots where needed.
3. Disable the Cloudflare Cron Trigger to avoid duplicate snapshots.
4. Do not delete the raw snapshots already collected.

This emergency collector intentionally does not replace canonical event discovery. It only collects events already present in `ticket_events`.
