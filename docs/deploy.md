# Deploying

## Read this first: Vercel is a compromise for this app

Vercel runs Python as serverless functions, and two properties of that model
work against what this tool is for.

**The filesystem is ephemeral.** Only `/tmp` is writable, and it does not
outlive the instance. The evidence archive is therefore not durable: a run is
retrievable by id until the next cold start, after which
`GET /api/valuations/{run_id}` returns 404. The valuation, the memo and the
complete audit trail still come back in the response body, so nothing is lost
from any individual request. What is lost is the archive, which is one of the
things that makes the tool worth having.

**Live runs are slow.** A full sector screen reads filings for eighteen
companies one at a time and takes roughly 13 to 20 seconds. The bundled
`vercel.json` sets `maxDuration` to 60 seconds, which is enough on a Hobby or
Pro plan, but it is close enough to the ceiling to be uncomfortable, and cold
starts add to it. Setting `data_mode` to `fixtures` sidesteps this entirely and
responds in milliseconds.

If either of those matters, a container host with a persistent volume is a
better fit. Render, Railway and Fly.io all run this unchanged with a single
start command and no serverless caveats:

```
uvicorn vc_audit.api.main:app --host 0.0.0.0 --port $PORT
```

With that said, Vercel works, and the configuration is committed.

## Deploying to Vercel

Everything needed is already in the repository: `vercel.json`,
`requirements.txt`, `.vercelignore`, and `api/index.py`.

### From the dashboard

1. Go to [vercel.com/new](https://vercel.com/new) and import
   `riyapawar/vc-audit`.
2. Leave every build setting on its default. Vercel detects `api/index.py` as a
   Python function and `vercel.json` supplies the rest.
3. Click **Deploy**.

### From the command line

```bash
npm i -g vercel
vercel          # preview deployment
vercel --prod   # production
```

### Optional environment variables

Set these under **Settings, Environment Variables**. None is required.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Enables the research toggle in the deployed UI. Also add `anthropic>=0.69` to `requirements.txt`, which is deliberately left out so a deployment that never uses it does not pay the cold-start cost. |
| `SEC_USER_AGENT` | Identifies your deployment to SEC EDGAR, in the form `Your Name you@example.com`. A working default is used if unset. |

`VC_AUDIT_OUTPUT_DIR` and `VC_AUDIT_EXAMPLES_DIR` are already set in
`vercel.json` and should not need changing.

### What the configuration does

`vercel.json` rewrites every path to the single ASGI function, so the web UI,
the JSON API and `/docs` are all served by one deployment. `api/index.py` puts
`src/` on the import path, because a serverless bundle does not do the
equivalent of `pip install -e .`, and then re-exports the same `app` object that
`vc-audit serve` runs. It adds no behaviour of its own, so the deployed service
and a local run cannot diverge.

## Verifying a deployment

```bash
curl https://<your-deployment>.vercel.app/healthz
curl "https://<your-deployment>.vercel.app/api/peers?sector=saas&data=fixtures"
```

```bash
curl -X POST https://<your-deployment>.vercel.app/api/valuations \
  -H 'Content-Type: application/json' \
  -d '{"company": {"name": "Basis AI", "sector": "saas", "ltm_revenue_usd": 10000000},
       "as_of": "2026-08-21", "data_mode": "fixtures"}'
```

The web UI is at the root, and the OpenAPI documentation at `/docs`.

## Making the archive durable

The one change worth making for a real deployment is replacing the filesystem
archive. `vc_audit.reporting.evidence` is the only module that touches disk, and
it has four functions: `write`, `load_report`, `list_runs` and a dataclass. An
S3 or database implementation of those four is the whole job, and nothing above
that layer needs to change.
