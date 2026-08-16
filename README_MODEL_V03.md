# Model A v0.3

Historical-only attendance forecasting experiment.

## Scope
Uses Lech Poznań home attendance and adds objectively reconstructed sporting
context from Ekstraklasa results.

The enrichment intentionally starts at 2017/18:
- more comparable modern demand era,
- avoids older league points-halving rules,
- enough observations remain for chronological testing.

## Added pre-match features
- league positions
- points
- points to leader
- PPG
- last-5 points
- last-5 goal difference
- round / matches remaining / season progress

No Roboticket/live inventory is used.

## Run
1. Execute `sql/historical_context_v03.sql` in Supabase once.
2. Upload scripts + workflow to GitHub.
3. Run `Build context and train Model A v0.3`.
4. Inspect artifact `attendance-model-v03`.
