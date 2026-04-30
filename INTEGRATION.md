# EcoTrace — Frontend ↔ Backend Integration Guide

This document explains how the React frontend (`fronend/`) connects to the
FastAPI backend (`backend/`) and how to run both together locally.

---

## Architecture

```
Browser
  │
  │  /api/*  (proxied by Vite dev server)
  ▼
FastAPI  (http://127.0.0.1:8000)
  ├── POST /api/search          ← main consumer search
  ├── POST /api/upload          ← PDF document upload
  ├── GET  /api/abn/verify/:abn
  ├── GET  /api/company/search/:name
  ├── GET  /api/barcode/:barcode
  ├── GET  /api/trademark/search/:brand
  ├── GET  /api/search/query/:query_id
  ├── GET  /api/search/history/:user_id
  └── GET  /health
```

During development, the **Vite dev server proxy** (`fronend/vite.config.ts`)
forwards all `/api/*` and `/health` requests to `http://127.0.0.1:8000`.
This means:
- No CORS configuration is needed in the browser.
- The frontend code never hard-codes a host (just `/api/search`).
- The backend's `allow_origins=["*"]` only matters in production.

---

## Centralised API Client

All fetch calls go through **`fronend/src/lib/api.ts`**.
Import the function you need — do not write raw `fetch()` calls in components.

```ts
import { search, verifyAbn, lookupBarcode, uploadDocument } from '@/lib/api';

// POST /api/search
const data = await search({ brand: 'Tim Tam' });
console.log(data.query_id, data.result);

// GET /api/abn/verify/88000014675
const abn = await verifyAbn('88000014675');

// POST /api/upload
const receipt = await uploadDocument(myPdfFile);
```

---

## Running Locally

### 1. Backend

```bash
cd backend
cp .env.example .env        # fill in DB_PASSWORD, ABR_GUID, IP_AUSTRALIA_*
pip install fastapi uvicorn psycopg2-binary python-dotenv requests python-multipart
uvicorn main:app --reload   # runs on http://127.0.0.1:8000
```

> **Note:** Register the upload router in `main.py`:
>
> ```python
> from upload_endpoint import router as upload_router
> app.include_router(upload_router)
> ```

### 2. Frontend

```bash
cd fronend
cp .env.example .env        # VITE_API_BASE_URL= (leave empty for local proxy)
pnpm install
pnpm dev                    # runs on http://localhost:5173
```

Open [http://localhost:5173/app/search](http://localhost:5173/app/search).
Searches will proxy to the FastAPI backend automatically.

---

## Production

Set the environment variable **before** building:

```bash
VITE_API_BASE_URL=https://api.ecotrace.example.com pnpm build
```

The built JS will call `https://api.ecotrace.example.com/api/search` etc.
Make sure the backend's CORS `allow_origins` includes your frontend domain.

---

## Environment Variables

### Frontend (`fronend/.env`)

| Variable | Default | Description |
|---|---|---|
| `VITE_API_BASE_URL` | `` (empty) | Backend base URL for production. Leave empty locally. |

### Backend (`backend/.env`)

| Variable | Description |
|---|---|
| `DB_HOST` | PostgreSQL host |
| `DB_PORT` | PostgreSQL port (default 5432) |
| `DB_NAME` | Database name |
| `DB_USER` | Database user |
| `DB_PASSWORD` | Database password |
| `ABR_GUID` | ABR Web Services authentication GUID |
| `IP_AUSTRALIA_CLIENT_ID` | IP Australia OAuth client ID |
| `IP_AUSTRALIA_CLIENT_SECRET` | IP Australia OAuth client secret |
| `IP_AUSTRALIA_TOKEN_URL` | OAuth token endpoint URL |
| `IP_AUSTRALIA_TRADEMARK_URL` | Trademark search base URL |
| `UPLOAD_DIR` | Directory for uploaded PDFs (default `./uploads`) |
| `MAX_UPLOAD_MB` | Max upload size in MB (default `10`) |

---

## API Response Shape (`POST /api/search`)

```jsonc
{
  "query_id": "<uuid>",
  "status": "success",
  "input_type": "brand",          // barcode | brand | company_or_abn
  "input_value": "Tim Tam",
  "resolution_status": "resolved", // pending | resolved | failed
  "pipeline_steps": ["IP Australia Trade Mark Search", "ABR company name lookup"],
  "result": {
    "input_type": "brand",
    "confidence": 80,
    "brand": { "brand_name": "Tim Tam" },
    "legal_owner": "ARNOTT'S BISCUITS LIMITED",
    "abn_verification": { "abn": "34 003 645 529", "legal_name": "...", ... },
    "trademark": { ... },
    // Scoring fields added by the report layer (not yet implemented):
    "risk_score": null,
    "risk_factors": [],
    "alternatives": []
  }
}
```
