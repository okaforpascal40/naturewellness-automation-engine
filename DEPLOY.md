# Deployment Guide — NatureWellness Automation Engine

## Environment Variables

Set these in Railway (or your platform's secret manager). **Never commit `.env`.**

| Variable | Required | Description |
|---|---|---|
| `SUPABASE_URL` | Yes | Project URL from Supabase Dashboard → Settings → API |
| `SUPABASE_SERVICE_KEY` | Yes | Service Role key (not anon key) — full DB access |
| `USDA_API_KEY` | Yes | Free key from https://fdc.nal.usda.gov/api-guide.html |
| `OPEN_TARGETS_API_URL` | No | Defaults to `https://api.platform.opentargets.org/api/v4/graphql` |
| `REACTOME_API_URL` | No | Defaults to `https://reactome.org/ContentService` |
| `CHEMBL_API_URL` | No | Defaults to `https://www.ebi.ac.uk/chembl/api/data` |
| `FOODB_API_URL` | No | Defaults to `https://foodb.ca` |
| `APP_ENV` | No | `production` or `development` (default: `development`) |
| `LOG_LEVEL` | No | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default: `INFO`) |
| `CORS_ORIGINS` | No | Comma-separated allowed frontend origins |

## Supabase Setup

1. Create a project at https://supabase.com
2. Go to **Settings → API** and copy:
   - **Project URL** → `SUPABASE_URL`
   - **service_role** secret → `SUPABASE_SERVICE_KEY`
3. Run the database migrations (SQL files in `supabase/migrations/` if present) via the Supabase SQL editor or CLI:
   ```bash
   supabase db push
   ```
4. Verify the required tables exist: `genes`, `compounds`, `food_sources`, and junction tables.

## Railway Deployment

### First-time setup

1. Install the Railway CLI:
   ```bash
   npm install -g @railway/cli
   railway login
   ```

2. Create a new project and link this repo:
   ```bash
   railway init
   # or link an existing project:
   railway link
   ```

3. Set environment variables:
   ```bash
   railway variables set SUPABASE_URL=https://your-ref.supabase.co
   railway variables set SUPABASE_SERVICE_KEY=your_service_role_key
   railway variables set USDA_API_KEY=your_usda_key
   railway variables set APP_ENV=production
   railway variables set LOG_LEVEL=INFO
   ```

4. Deploy:
   ```bash
   railway up
   ```

### Subsequent deploys

Push to the linked branch — Railway auto-deploys on push.

### Health check

Railway is configured to hit `/health` to verify the service is up (see `railway.toml`).
Confirm the endpoint is live:
```bash
curl https://your-railway-domain.railway.app/health
```

## Local Development

```bash
# 1. Create and activate virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with your credentials

# 4. Run the server
uvicorn app.main:app --reload --port 8000
```

API docs available at: http://localhost:8000/docs
