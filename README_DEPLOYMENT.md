# Railway Deployment — NatureWellness Automation Engine

Production URL: `https://naturewellness-api.railway.app`

## Environment Variables

Set these in Railway → Project → Variables:

| Variable | Required | Where to get it |
|---|---|---|
| `SUPABASE_URL` | Yes | Supabase Dashboard → Settings → API → Project URL |
| `SUPABASE_KEY` | Yes | Supabase Dashboard → Settings → API → service_role secret |
| `ANTHROPIC_API_KEY` | Yes | https://console.anthropic.com → API Keys |
| `USDA_API_KEY` | Yes | https://fdc.nal.usda.gov/api-guide.html (free) |
| `APP_ENV` | No | Set to `production` |
| `LOG_LEVEL` | No | `INFO` for production, `DEBUG` for troubleshooting |
| `CORS_ORIGINS` | No | Your frontend URL, e.g. `https://naturewellness.app` |

## Supabase Setup

1. Go to https://supabase.com and create a project
2. In **Settings → API**, copy:
   - **Project URL** → `SUPABASE_URL`
   - **service_role** key → `SUPABASE_KEY`
3. Run migrations in the Supabase SQL editor to create the required tables:
   - `genes`, `compounds`, `food_sources`
   - `disease_gene_associations`, `gene_pathway_mappings`
   - `compound_gene_interactions`, `food_compound_mappings`
   - `evidence_scores`, `review_records`

## Railway Deployment Steps

### Option A — GitHub Integration (recommended)

1. Push this repo to GitHub
2. Go to https://railway.app → New Project → Deploy from GitHub repo
3. Select this repository
4. Railway auto-detects the `Procfile` and deploys
5. Add environment variables under **Variables** tab
6. Railway assigns a public URL automatically

### Option B — Railway CLI

```bash
npm install -g @railway/cli
railway login
railway init          # or: railway link
railway variables set SUPABASE_URL=https://...
railway variables set SUPABASE_KEY=...
railway variables set ANTHROPIC_API_KEY=...
railway variables set USDA_API_KEY=...
railway variables set APP_ENV=production
railway up
```

## Verify Deployment

```bash
# Health check
curl https://naturewellness-api.railway.app/health

# API docs
open https://naturewellness-api.railway.app/api/v1/docs

# Test automation endpoint
curl -X POST https://naturewellness-api.railway.app/api/v1/automation/run \
  -H "Content-Type: application/json" \
  -d '{"disease_id": "C0011849", "disease_name": "Diabetes Mellitus, Type 2"}'
```

## Local Development

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # fill in your credentials
uvicorn app.main:app --reload --port 8000
```

Docs available at: http://localhost:8000/api/v1/docs
