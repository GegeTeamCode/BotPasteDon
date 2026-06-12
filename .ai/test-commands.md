# Test & Verify Commands — BotPasteDon

**Heads up:** this project has **no automated test suite, no CI/CD, no
typecheck**. "Done" means: code compiles, deploy to bot prod (or `--once`
against an isolated DB), and observe live behavior. Adapt accordingly.

## Local pre-flight (do these before SCP'ing anything)

```bash
# Syntax check a single file
python -c "import py_compile; py_compile.compile('PATH/TO/FILE.py', doraise=True); print('OK')"

# Check the whole package (no test runner; just imports)
python -c "import status_sync.main, status_sync.g2g_sync, status_sync.eldo_sync; print('OK')"
```

There is `tests/` but it contains paramiko-driven dev probes (e.g.
`tests/test_g2g_api.py`, `tests/check_pending.py`), not pytest cases.
Treat them as scratch scripts.

## Live process health

```bash
# Top-line: all 9 services up + no duplicates + ports + heartbeat
python scripts/check_all_processes.py
# Note: as of 2026-06-10 this script still lists "8 services" — it
# hasn't been updated for status_sync. Read it accordingly.

# Auth /health
ssh root@192.168.2.220 'curl -s http://localhost:8010/health | python -m json.tool'

# Phase 4 Eldo + Phase 5 G2G refresh cycles (each should fire ~every 13min)
ssh root@192.168.2.220 'grep -E "\[(ELDO|G2G)\] (Trying backend|backend refresh)" /tmp/auth*.log | tail -20'

# Recent scanner pastes (last hour)
ssh root@192.168.2.220 'grep "ERP accepted" /tmp/g2g_scanner.log /tmp/eldo_scanner.log | tail -20'

# DB pending state
ssh root@192.168.2.220 'cd /opt/BotPasteDon && venv/bin/python -c "
import sqlite3
c = sqlite3.connect(\"data/orders.db\")
for s in [\"DETECTED\",\"DELIVERING\",\"COMPLETED\",\"FAILED\"]:
    print(s, c.execute(\"SELECT count(*) FROM orders WHERE status=?\", (s,)).fetchone()[0])
print(\"erp_unsynced:\", c.execute(\"SELECT count(*) FROM orders WHERE erp_synced=0 AND status NOT IN (\\\"DETECTED\\\",\\\"FAILED\\\")\").fetchone()[0])
"'
```

## status_sync verification (when changing G2G/Eldo state sync)

```bash
# Test a single cycle against ISOLATED DB (won't touch prod state):
ssh root@192.168.2.220 'cd /opt/BotPasteDon && \
    DATABASE_PATH=/tmp/verify.db \
    ERP_STATUS_UPDATE_URL=http://127.0.0.1:1/discard \
    ERP_API_KEY_G2G=test_secret_g2g_456 \
    ERP_API_KEY_ELDO=test_secret_eldo_123 \
    venv/bin/python -u -m status_sync --once 2>&1 | tail -50'

# After: verify which rows landed
ssh root@192.168.2.220 'venv/bin/python -c "
import sqlite3
c = sqlite3.connect(\"/tmp/verify.db\")
print(\"total status:\", c.execute(\"SELECT count(*) FROM marketplace_status\").fetchone()[0])
for r in c.execute(\"SELECT platform, marketplace_state, count(*) FROM marketplace_status GROUP BY platform, marketplace_state ORDER BY 1,2\"):
    print(r)
"'

# Clean up
ssh root@192.168.2.220 'rm -f /tmp/verify.db /tmp/verify.db-wal /tmp/verify.db-shm'
```

## ERP webhook (dev) verification

`scripts/_test_status_update_webhook.py` (untracked scratch) runs 11 mapping
rules against ERP dev (`192.168.2.228` site `test.localhost`). Last full
run: 11/11 PASS (2026-06-06). Re-run after touching state mapping logic.

## Eldo auth deep-dive

```bash
# Cookie state in a profile (offline — won't disturb the live session)
ssh root@192.168.2.220 'venv/bin/python -c "
import sqlite3, datetime
c = sqlite3.connect(\"/opt/BotPasteDon/chrome_profile_eldo/cookies.sqlite\")
for n in (\"__Host-EldoradoIdToken\",\"__Host-EldoradoRefreshToken\",\"__Host-XSRF-TOKEN\"):
    r = c.execute(\"SELECT expiry FROM moz_cookies WHERE name=?\", (n,)).fetchone()
    print(n, datetime.datetime.utcfromtimestamp(r[0]) if r else \"MISSING\")
"'

# Force a single capture cycle for diagnosis (auth keeps running)
ssh root@192.168.2.220 'curl -s --max-time 60 http://localhost:8010/auth/eldo | python -m json.tool | head -10'
```

## Definition of done

For any change touching shipped behavior:

1. Local syntax check passes (`py_compile`).
2. Deploy to bot prod via the matching `scripts/_deploy_*.py` script.
3. `python scripts/check_all_processes.py` — all services OK, no DUP.
4. Auth `/health` — `logged_in: true` (Eldo) + `has_jwt: true` (G2G).
5. Scanner pastes at least one new order without 401 spam (tail logs ≥15
   min after deploy).
6. If touching `status_sync` or ERP webhook: dev-ERP verification + one
   prod `--once` cycle with isolated DB before enabling the long-running
   process.
