# Upstash Redis for NINA sessions

NINA stores per-visitor session state (cart flow, turn history, pending clarifications).
On Render with multiple workers, in-memory sessions are lost between requests. **Redis
fixes this** by sharing session state across all workers.

## 1. Create an Upstash database

1. Sign in at [console.upstash.com](https://console.upstash.com).
2. **Create Database** → pick a region close to your Render service (e.g. `us-east-1`).
3. On the database page, copy the **Redis URL** (not the REST URL).

   It looks like:

   ```
   rediss://default:AbCdEf...@us1-xxx.upstash.io:6379
   ```

   Upstash uses TLS (`rediss://`). If you only see `redis://`, NINA auto-upgrades
   Upstash hosts to TLS.

## 2. Set `NINA_REDIS_URL` on Render

1. Open [Render Dashboard](https://dashboard.render.com) → **nina-console** → **Environment**.
2. Add:

   | Key | Value |
   |-----|-------|
   | `NINA_REDIS_URL` | Your Upstash **TCP** Redis URL |

   Paste **only** the URL — no quotes, no `REDIS_URL=` prefix:

   ```
   rediss://default:YOUR_TOKEN@wise-snipe-142190.upstash.io:6379
   ```

   Common mistake: copying `REDIS_URL="rediss://..."` from Upstash includes
   extra characters that break the connection.

3. Save and redeploy (or trigger **Manual Deploy**).

`UPSTASH_REDIS_URL` is also accepted if your integration sets that name instead.

## 3. Verify

After deploy, open:

```
https://nina-console.onrender.com/health
```

Expected when Redis is working:

```json
{
  "ok": true,
  "redis": {
    "configured": true,
    "ok": true,
    "provider": "upstash"
  }
}
```

If `NINA_REDIS_URL` is set but unreachable, `ok` is `false` and `redis.ok` is `false`.

## 4. Local development (optional)

**Docker Compose** — set in `.env`:

```
NINA_REDIS_URL=rediss://default:TOKEN@....upstash.io:6379
```

**Local Redis** (no TLS):

```
NINA_REDIS_URL=redis://localhost:6379
```

Install the client:

```bash
pip install redis
```

## How it works

- `NinaPool` calls `shared_redis_store()` once per process when `NINA_REDIS_URL` is set.
- Sessions are stored under keys `nina:sess:{sessionId}` with TTL from `expiresAt`.
- Without Redis, sessions use in-memory `MemoryStore` (fine for single-worker dev).
- The widget still sends `cartFlow` in `session_hints` as a safety net if Redis is down.

## Multi-worker on Render

With Postgres (`DATABASE_URL`) + Redis (`NINA_REDIS_URL`), you can raise workers:

```
UVICORN_WORKERS=2
```

in Render environment variables.
