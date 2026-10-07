# New Supabase migration v0.1

Purpose: move Beyond/ticket-intelligence from a quota-restricted Supabase organization to a fresh project without carrying over the high-egress architecture.

## 1. Bootstrap the new project

In the NEW Supabase project, open SQL Editor and run:

`sql/new_project_bootstrap_v01.sql`

Expected tables:
- `ticket_events`
- `snapshots`
- `sector_inventory`
- `forecast_observations`
- `ticket_event_outcomes`

The schema is aggregate-first: `snapshots.available_total` and `snapshots.sector_count` exist from day one.

## 2. Export only minimal state from the old project

The old project remains accessible through the Supabase Dashboard even while API services are quota-restricted.

Open SQL Editor in the OLD project and use:

`sql/new_project_minimal_export_v01.sql`

Run the four SELECT statements separately and download each result as CSV:
1. `ticket_events.csv`
2. `snapshots.csv`
3. `ticket_event_outcomes.csv`
4. `forecast_observations.csv`

Do NOT export `sector_inventory`.

The snapshots export calculates `available_total` and `sector_count` server-side. The forecast export replaces large historical `payload` JSON blobs with `{}` because calibration uses scalar columns.

## 3. Import into the new project

Use Supabase Table Editor CSV import in this order:
1. `ticket_events`
2. `snapshots`
3. `ticket_event_outcomes`
4. `forecast_observations`

Keep the explicit `id` values from the CSVs so foreign keys remain valid.

## 4. Reset identity sequences

After import, run in the NEW project SQL Editor:

```sql
SELECT setval(pg_get_serial_sequence('ticket_events','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM ticket_events;
SELECT setval(pg_get_serial_sequence('snapshots','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM snapshots;
SELECT setval(pg_get_serial_sequence('forecast_observations','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM forecast_observations;
SELECT setval(pg_get_serial_sequence('sector_inventory','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM sector_inventory;
```

## 5. Update GitHub Actions secrets

Repository: `NiedzielX/ticket-intelligence`

GitHub → Settings → Secrets and variables → Actions.

Replace:
- `SUPABASE_URL` with the NEW project URL.
- `SUPABASE_SECRET_KEY` with a NEW backend secret/service-role key.

Do not paste either secret into chat, issues, commits, or workflow files.

Roboticket credentials do not change.

## 6. Smoke test

Run `Ticket Intelligence Pipeline v0.3` manually with `workflow_dispatch`.

Expected signals:
- new active events resolve successfully;
- collection creates new snapshots;
- `Snapshot aggregates used` is greater than zero;
- `Sector inventory fallbacks: 0` after imported snapshots/new writes have aggregates;
- forecast and monitor complete successfully.

Keep the emergency cadence (15-minute collection, ~6-hour intelligence) until new-project egress is observed for several days.

## 7. Do not delete the old project yet

Keep it as a temporary archive until:
- at least one new snapshot per active event exists in the new project;
- forecast observations are being persisted;
- historical calibration still sees the migrated completed events;
- new-project egress remains stable.
