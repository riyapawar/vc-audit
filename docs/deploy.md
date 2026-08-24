# Deploying

Railway is the recommended target, because it is the one that keeps the evidence
archive. Vercel configuration is also committed and works, with a caveat worth
understanding before you use it.

## Railway (recommended)

Everything needed is committed: `railway.json` sets the build and start
commands, and the app already reads its port and its output directory from the
environment.

### Deploy

1. Go to [railway.com/new](https://railway.com/new) and choose **Deploy from
   GitHub repo**.
2. Select `riyapawar/vc-audit`. Railway reads `railway.json` and needs no
   further build configuration.
3. Once the first deploy is green, open **Settings, Networking** and click
   **Generate Domain** to get a public URL.

Or from the command line:

```bash
npm i -g @railway/cli
railway login
railway init
railway up
```

### Give it a persistent volume

This is the step that makes Railway worth choosing over a serverless host, and
it is the only manual configuration required.

1. In the service, open **Settings, Volumes** and add a volume mounted at
   `/data`.
2. Under **Variables**, set `VC_AUDIT_OUTPUT_DIR` to `/data/out`.

Evidence packs now survive restarts and redeploys, so `vc-audit runs`,
`GET /api/valuations/{run_id}` and the archived memos keep working across the
life of the deployment rather than the life of one container.

Without the volume the service still runs correctly; the archive simply resets
whenever the container does.

### Environment variables

Set these under **Variables**. Only the first is meaningful for most
deployments, and none is required for the service to start.

| Variable | Purpose |
|---|---|
| `VC_AUDIT_OUTPUT_DIR` | `/data/out`, to put the evidence archive on the volume. |
| `SEC_USER_AGENT` | Identifies your deployment to SEC EDGAR, in the form `Your Name you@example.com`. EDGAR asks callers to identify themselves; a working default is used if unset. |
| `ANTHROPIC_API_KEY` | Enables the research toggle in the deployed UI. Also add `anthropic>=0.69` to `requirements.txt`, which is deliberately left out so a deployment that never uses it does not pay the install cost. |

`PORT` is supplied by Railway and must not be set by hand.

### What the configuration does

`buildCommand` is `pip install .` rather than the default requirements install,
because the package lives under `src/` and needs to be installed for
`uvicorn vc_audit.api.main:app` to resolve. The wheel carries the market data
fixtures and the web UI, so a built install is fully functional offline.

`healthcheckPath` points at `/healthz`, so a deploy that fails to boot is caught
by Railway rather than by the first user.

### Verify

```bash
curl https://<your-domain>.up.railway.app/healthz
```

```bash
curl -X POST https://<your-domain>.up.railway.app/api/valuations \
  -H 'Content-Type: application/json' \
  -d '{"company": {"name": "Basis AI", "sector": "saas", "ltm_revenue_usd": 10000000},
       "as_of": "2026-08-21", "data_mode": "fixtures"}'
```

The web UI is at the root and the OpenAPI documentation at `/docs`.

A live run reads filings for every candidate in the sector one at a time and
takes roughly 13 to 20 seconds. Railway has no request timeout to worry about,
but the first request after a cold start also pays for the SEC ticker map
download. Passing `"data_mode": "fixtures"` responds in milliseconds and is the
right choice for a quick demonstration.

## Vercel (works, with a caveat)

`vercel.json`, `requirements.txt` and `api/index.py` are committed, and the
deployment works: import the repo at [vercel.com/new](https://vercel.com/new)
and accept every default, or run `vercel --prod`.

The caveat is that serverless filesystems are ephemeral. Only `/tmp` is
writable and it does not outlive the instance, so **the evidence archive is not
durable there**: a run is retrievable by id until the next cold start, after
which `GET /api/valuations/{run_id}` returns 404. Every response still carries
the complete valuation, memo and audit trail, so no individual request loses
anything. What is lost is the archive, which is one of the things that makes
this tool worth having.

Live runs are also close to the ceiling. A full sector screen takes 13 to 20
seconds against the 60 second `maxDuration` set in `vercel.json`, and cold
starts add to that.

`api/index.py` puts `src/` on the import path, because a serverless bundle does
not do the equivalent of `pip install -e .`, then re-exports the same `app`
object that `vc-audit serve` runs. It adds no behaviour, so the deployed service
and a local run cannot diverge.

## Any container host

The service is a plain ASGI app with no host-specific code, so Render, Fly.io or
a container of your own need only:

```bash
pip install .
uvicorn vc_audit.api.main:app --host 0.0.0.0 --port $PORT
```

Point `VC_AUDIT_OUTPUT_DIR` at a mounted volume and the archive persists.

## Making the archive durable elsewhere

If you would rather not depend on a volume at all, `vc_audit.reporting.evidence`
is the only module that touches disk, and it has three functions plus a
dataclass: `write`, `load_report` and `list_runs`. An S3 or database
implementation of those three is the whole job, and nothing above that layer
changes.
