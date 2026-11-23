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