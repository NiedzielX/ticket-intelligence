# v0.4 — authenticated Roboticket collector

Changes:
- logs into the normal Roboticket/Legia supporter account before opening the event;
- credentials are read only from GitHub Secrets;
- captures WGL seat / occupancy responses after login;
- uploads a short-lived GitHub artifact with screenshot, HTML and console diagnostics;
- does not write cookies, tokens or credentials to logs.

Required GitHub repository secrets:

- `SUPABASE_URL`
- `SUPABASE_SECRET_KEY`
- `ROBOTICKET_USERNAME`
- `ROBOTICKET_PASSWORD`

First run remains manual (`workflow_dispatch`). No schedule is enabled yet.
