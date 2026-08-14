# Ticket Intelligence v0.2

GitHub Actions -> Playwright -> Roboticket -> Supabase.

## First test

1. Replace `collector.py` and `requirements.txt` in the repository.
2. Add `.github/workflows/collect.yml`.
3. Open GitHub -> Actions -> Collect Legia inventory.
4. Click Run workflow.
5. Inspect the log.
6. In Supabase SQL Editor run:

```sql
select * from snapshots order by captured_at desc limit 10;

select occ, count(*)
from seat_occupancy
where snapshot_id = (select max(id) from snapshots)
group by occ
order by occ;
```

The workflow is manual only. No schedule is enabled yet.

## Important

v0.2 does not interpret `occ` as sold/available. It stores Roboticket's raw state.
Sector traversal is best-effort until we validate the DOM behaviour in GitHub's headless Chromium.
