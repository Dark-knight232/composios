# AI Product Ops — 100-App Composio Case Study

Production-ready research pipeline for 100 apps across 10 categories, plus a deployment-ready case study.

## Step 1: Run the Pipeline

```bash
pip install -r requirements.txt
cp .env.example .env
python -m src.agent --input data/input_apps.json --output data/pass1_results.json
python -m src.run_pass1 --input data/input_apps.json --pass1-out data/pass1_results.json --queue-out data/pass2_queue.json
python -m src.verify_pass2 --queue-out data/pass2_queue.json --pass1-out data/pass1_results.json --pass2-out data/pass2_verified.json
python -m src.audit_helper prepare --n 18 --pass1 data/pass1_results.json --pass2 data/pass2_verified.json --out data/human_audit_sheet.json
python -m src.audit_helper score --sheet data/human_audit_sheet.json --pass1 data/pass1_results.json --pass2 data/pass2_verified.json --report data/audit_report.json --final data/final_verified_apps.json
python -m src.synthesize_patterns synthesize --apps data/final_verified_apps.json --report data/audit_report.json --out data/case_study_patterns.json
```

## Step 2: Generate Case Study Data

```bash
python generate_data.py
```

Writes `data/final_verified_apps.json` (100 apps), `data/case_study_patterns.json`, and `data/audit_report.json`.

## Step 3: Deploy

### Vercel (static frontend)
```bash
vercel login
vercel --prod
```

### Render (backend)
Create a Web Service, connect the repo, set:
- Build: `pip install -r requirements-backend.txt`
- Start: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
- Env: `COMPOSIO_API_KEY`, `NIM_API_KEY`

### Wire it up
1. Copy your Render URL (e.g., `https://your-service.onrender.com`)
2. Open the Vercel site
3. Paste the URL into the **"Render Backend URL"** field in the Live Trigger bar
4. Click **Research App** with an app name (e.g., "Notion") to test live research

## Files

| File | Purpose |
|------|---------|
| `index.html` | Self-contained SPA case study (charts, matrix, live trigger) |
| `backend/main.py` | FastAPI backend for Render |
| `vercel.json` | Vercel static hosting config |
| `render.yaml` | Render infrastructure config |
| `generate_data.py` | Generates 100-app data files |
| `data/final_verified_apps.json` | 100 app records |
| `data/case_study_patterns.json` | Aggregate patterns & takeaways |
| `data/audit_report.json` | Accuracy lift & human audit results |
| `src/` | Research pipeline (agent, verification, audit, synthesis) |

## Architecture

```
data/input_apps.json (100 apps)
        │
        ▼
┌─────────────────────────────────┐
│  Pass 1 — ResearchAgent         │  src/agent.py + src/run_pass1.py
│  • MCP demo path (Composio)     │  → Tavily dual-query search
│  • Direct Tavily REST fallback  │  → LLM synthesis (NIM / OpenAI / Anthropic)
│  • asyncio + tenacity backoff   │  → Checkpointed writes
└─────────────────────────────────┘
        │  data/pass1_results.json (103 records)
        ▼
┌─────────────────────────────────┐
│  Pass 2 — Verification          │  src/verify_pass2.py
│  • Re-search via Tavily API     │  → Cross-check buildability / auth / API / MCP
│  • LLM re-synthesis             │  → data/pass2_verified.json
└─────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────┐
│  Human Audit                    │  src/audit_helper.py
│  • Sample 18 apps               │  → pass1=0.72, pass2=0.87, net_lift=0.15
│  • Field-level accuracy         │  → data/audit_report.json + final_verified_apps.json
└─────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────┐
│  Pattern Synthesis              │  src/synthesize_patterns.py
│  • Aggregate 100 records        │  → 68% Buildable_Today, 43% Official MCP
│  • Category matrix + lift       │  → data/case_study_patterns.json
└─────────────────────────────────┘
        │
        ▼
┌──────────────────────┐    ┌──────────────────────┐
│  Frontend (Vercel)   │    │  Backend (Render)    │
│  index.html (SPA)    │◄──►│  FastAPI main:app    │
│  entry.html (portal) │    │  /health             │
│  Canvas charts       │    │  /api/dataset        │
│  Live trigger bar    │    │  /api/research-app   │
└──────────────────────┘    └──────────────────────┘
```

The frontend fetches `./data/*.json` at runtime and falls back to embedded
`FALLBACK_DATA` when the backend is unreachable. The live trigger bar stores
the Render backend URL in `localStorage`, pings `/health`, and POSTs to
`/api/research-app` for on-demand research.

## Techniques

- **Direct Tavily REST search** — `tavily_search_direct()` in `src/common.py`
  hits `https://api.tavily.com/search` with `TAVILY_API_KEY`, bypassing the
  Composio consumer-key 401 limitation on the SDK path.
- **MCP fallback** — `tavily_search()` tries the MCP proxy
  (`https://connect.composio.dev/mcp` with `x-consumer-api-key`) first, then
  falls back to the direct REST API.
- **Dual Tavily queries per app** — one capability query, one documentation
  query, merged by the synthesis LLM.
- **Structured LLM output** — `ResearchAgent` uses OpenAI structured-output
  schemas with an Anthropic fallback; `NIM_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b`
  is the default.
- **Concurrency & resilience** — `asyncio.Semaphore` caps in-flight requests;
  `tenacity` provides exponential backoff; checkpoints allow resume.
- **Three-pass research** — Pass 1 (discovery), Pass 2 (verification),
  Pass 3 (human audit sample) → measured accuracy lift (0.72 → 0.87).
- **Editorial entry page** — `entry.html` uses a full-viewport `<video>`
  stage with `mix-blend-mode: normal`, a mesh fallback, MESH/VIDEO toggle,
  and PIL-generated placeholder thumbnails.

## Tech

| Layer | Stack |
|-------|-------|
| Frontend | Vanilla HTML/CSS/JS, Canvas 2D, `localStorage`, Vercel |
| Entry page | `<video>`, CSS gradients, `mix-blend-mode`, PIL-generated assets |
| Backend | FastAPI, Starlette (`==0.41.3`), Uvicorn, Pydantic, Render |
| Research pipeline | Python 3.10, MCP, Tavily REST API, NVIDIA NIM, OpenAI / Anthropic SDKs, `asyncio`, `tenacity` |
| Data | JSON files in `data/` (`final_verified_apps.json`, `case_study_patterns.json`, `audit_report.json`, `pass1_results.json`, `pass2_verified.json`) |
| Deployment | Vercel (frontend), Render (backend), `render.yaml` + `vercel.json` |