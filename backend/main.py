"""
EcoTrace Backend API
====================

Purpose
-------
This backend implements the consumer search pipeline for EcoTrace.

Consumer flow
-------------
The consumer flow is query_id based:
- Consumers do not need to log in.
- Each valid search creates exactly one search_query record.
- The backend returns query_id to the frontend.
- The report module can later use query_id to generate the risk report.

Supported frontend inputs
-------------------------
Only ONE input type should be submitted per request:
1. barcode       -> barcode_pipeline.py   (EAN-13 validate, OpenFoodFacts, GS1, ABR)
2. brand         -> brand_pipeline.py     (IP Australia Trademark, ABR)
3. company_or_abn -> abn_pipeline.py      (ABN checksum, ABR ABN lookup / name search)

Pipeline modules
----------------
  barcode_pipeline.py  : EAN-13 checksum -> OpenFoodFacts -> GS1 fallback -> ABR
  brand_pipeline.py    : IP Australia Trade Mark Search -> ABR
  abn_pipeline.py      : ABN checksum (ATO mod-89) -> ABR ABN lookup
                         OR company name -> ABR name search

All ABR Web Services calls live in abn_pipeline.py.
Barcode and brand pipelines receive abr_lookup_fn as a dependency.

Not included in this file
-------------------------
Report generation is intentionally not implemented here. The report team can
use query_id and search_query records produced by this backend.

Main endpoint
-------------
POST /api/search

Version history
---------------
5.0.0 - abn_pipeline.py extracted; ABR functions removed from main.py
4.0.0 - barcode and brand logic moved to dedicated pipeline modules
3.0.0 - initial pipeline implementation
"""

import os
import re
from typing import Optional, Dict, Any, Tuple, List
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ------------------------------------------------------------------
# Pipeline modules — one file per input type.
# All ABR Web Services calls live in abn_pipeline; brand and barcode
# pipelines receive search_company_name_with_abr as a dependency.
# ------------------------------------------------------------------
from abn_pipeline import (
    run_company_abn_phase,
    verify_abn_with_abr,
    search_company_name_with_abr,
    clean_abn,
    is_abn,
)
from barcode_pipeline import run_barcode_phase
from brand_pipeline import run_brand_phase, get_ip_australia_access_token


# ============================================================
# Environment
# ============================================================

load_dotenv()


# ============================================================
# FastAPI App
# ============================================================

app = FastAPI(
    title="EcoTrace Backend API",
    version="5.0.0",
    description="""
EcoTrace consumer search API.

Pipeline modules:
- abn_pipeline.py     : ABN checksum (ATO mod-89) + ABR lookup / name search
- barcode_pipeline.py : EAN-13 validation -> OpenFoodFacts -> GS1 -> ABR
- brand_pipeline.py   : IP Australia Trade Mark Search -> ABR

Every valid search creates a query_id and stores one search_query record.
"""
)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace with frontend deployment URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Database
# ============================================================

def get_conn():
    """
    Create a PostgreSQL database connection.

    Required .env variables:
    - DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
    """
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "ecotrace"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD"),
        cursor_factory=RealDictCursor,
    )


def serialize_row(row):
    """Convert a DB row to a JSON-safe dict (UUID -> str)."""
    if row is None:
        return None
    return {k: str(v) if isinstance(v, UUID) else v for k, v in row.items()}


# ============================================================
# Request Models
# ============================================================

class SearchRequest(BaseModel):
    """
    Main frontend search request.

    Exactly one of barcode, brand, or company_or_abn must be provided.

    Examples:
        {"barcode": "9310055002440"}
        {"brand": "Tim Tam"}
        {"company_or_abn": "Arnott's"}
        {"company_or_abn": "88000014675"}
    """
    user_id:        Optional[str] = None
    barcode:        Optional[str] = None
    brand:          Optional[str] = None
    company_or_abn: Optional[str] = None


class CreateUserRequest(BaseModel):
    """Development endpoint payload (backward compatibility)."""
    email:     str
    user_type: str = "consumer"


# ============================================================
# Input Cleaning and Validation
# ============================================================

def clean_text(value: Optional[str]) -> Optional[str]:
    """Strip whitespace; normalise empty strings to None."""
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def get_single_input_type(
    barcode:        Optional[str],
    brand:          Optional[str],
    company_or_abn: Optional[str],
) -> Tuple[str, str, str]:
    """
    Enforce exactly one input per request.

    Returns (input_type, input_value, frontend_type).
    Raises HTTP 400 on zero inputs, multiple inputs, or format errors.

    input_type values match search_query.input_type_enum:
        barcode | brand_name | company_name
    """
    provided = []
    if barcode:        provided.append(("barcode",      barcode,        "barcode"))
    if brand:          provided.append(("brand_name",   brand,          "brand"))
    if company_or_abn: provided.append(("company_name", company_or_abn, "company_or_abn"))

    if len(provided) == 0:
        raise HTTPException(
            status_code=400,
            detail="Please provide exactly one input: barcode, brand, or company_or_abn.",
        )
    if len(provided) > 1:
        raise HTTPException(
            status_code=400,
            detail="Please submit only one input type per search.",
        )

    input_type, input_value, frontend_type = provided[0]

    if input_type == "barcode":
        cleaned_bc = re.sub(r"[\s\-]", "", input_value)
        if not cleaned_bc.isdigit() or not (8 <= len(cleaned_bc) <= 14):
            raise HTTPException(
                status_code=400,
                detail="Invalid barcode. Must be 8-14 digits (EAN-8, EAN-13, ITF-14).",
            )

    if input_type == "brand_name" and len(input_value) < 2:
        raise HTTPException(status_code=400, detail="Brand name must be at least 2 characters.")

    if input_type == "company_name":
        cleaned = clean_abn(input_value)   # from abn_pipeline
        if cleaned.isdigit() and not is_abn(cleaned):
            raise HTTPException(status_code=400, detail="Invalid ABN — must be exactly 11 digits.")
        if not cleaned.isdigit() and len(input_value.strip()) < 2:
            raise HTTPException(status_code=400, detail="Company name must be at least 2 characters.")

    return input_type, input_value, frontend_type


# ============================================================
# search_query Lifecycle
# ============================================================

def create_search_query(cur, input_type: str, input_value: str, user_id: Optional[str] = None):
    """Insert a pending search_query before running any external API calls."""
    cur.execute(
        """
        INSERT INTO search_query (user_id, input_type, input_value, resolution_status)
        VALUES (%s, %s, %s, 'pending')
        RETURNING query_id, submitted_at;
        """,
        (user_id, input_type, input_value),
    )
    return cur.fetchone()


def update_search_query(
    cur,
    query_id,
    status:              str,
    resolved_company_id: Optional[str] = None,
    resolved_brand_id:   Optional[str] = None,
    resolved_product_id: Optional[str] = None,
):
    """Update search_query to 'resolved' or 'failed' after the pipeline completes."""
    if status not in ("resolved", "failed"):
        status = "failed"
    cur.execute(
        """
        UPDATE search_query
        SET resolution_status   = %s,
            resolved_company_id = %s,
            resolved_brand_id   = %s,
            resolved_product_id = %s
        WHERE query_id = %s;
        """,
        (status, resolved_company_id, resolved_brand_id, resolved_product_id, query_id),
    )


# ============================================================
# Basic Endpoints
# ============================================================

@app.get("/")
def root():
    return {
        "message":       "EcoTrace backend is running",
        "main_endpoint": "POST /api/search",
        "consumer_flow": "query_id based, no login required",
        "version":       "5.0.0",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ============================================================
# Development User Endpoint
# ============================================================

@app.post("/api/users/test")
def create_test_user(payload: CreateUserRequest):
    """Create or reuse a test user (backward compatibility, login not required)."""
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO "user" (user_type, email)
            VALUES (%s, %s)
            ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
            RETURNING user_id, user_type, email, created_at;
            """,
            (payload.user_type, payload.email),
        )
        user = cur.fetchone()
        conn.commit()
        return {"message": "User ready", "user": serialize_row(user)}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ============================================================
# Main Consumer Search Endpoint
# ============================================================

@app.post("/api/search")
def search_entity(payload: SearchRequest):
    """
    Main consumer search endpoint.

    Steps
    -----
    1. Validate exactly one input type.
    2. Insert a pending search_query record (obtains query_id).
    3. Dispatch to the appropriate pipeline module.
    4. Update search_query to resolved / failed.
    5. Return a unified JSON response.

    Pipeline dispatch
    -----------------
    barcode       -> barcode_pipeline.run_barcode_phase()
    brand_name    -> brand_pipeline.run_brand_phase()
    company_name  -> abn_pipeline.run_company_abn_phase()  (auto-detects ABN vs name)
    """
    barcode        = clean_text(payload.barcode)
    brand          = clean_text(payload.brand)
    company_or_abn = clean_text(payload.company_or_abn)

    input_type, input_value, frontend_type = get_single_input_type(
        barcode, brand, company_or_abn
    )

    conn = get_conn()
    cur  = conn.cursor()

    try:
        query    = create_search_query(cur, input_type, input_value, payload.user_id)
        query_id = query["query_id"]

        pipeline_steps: List[str] = []
        result:         Dict[str, Any] = {}
        db_status = "failed"

        # ----------------------------------------------------
        # Branch 1 — Company name / ABN
        # Auto-detects ABN vs name inside run_company_abn_phase.
        # ABN path: format check -> checksum -> ABR SearchByABN
        # Name path: validation -> ABR SearchByName
        # ----------------------------------------------------
        if input_type == "company_name":
            phase = run_company_abn_phase(company_or_abn)

            pipeline_steps = phase.get("pipeline", [])
            db_status      = "resolved" if phase["success"] else "failed"

            # Normalise result shape: both ABN and name paths expose `company`.
            company_block = phase.get("company") or {
                "legal_name":  phase.get("legal_name"),
                "abn":         phase.get("abn"),
                "entity_type": phase.get("entity_type"),
                "acn":         phase.get("acn"),
                "state":       phase.get("state"),
                "postcode":    phase.get("postcode"),
                "abn_status":  phase.get("abn_status"),
                "gst_registered": phase.get("gst_registered"),
                "gst_from_date":  phase.get("gst_from_date"),
                "main_activity":  phase.get("main_activity"),
            }

            result = {
                "input_type":    "abn"          if phase.get("valid_format") is not None else "company_name",
                "input_value":   input_value,
                "status":        phase.get("status", "not_found"),
                "source":        "ABR",
                "company":       company_block,
                "all_results":   phase.get("all_results", []),
                "total_results": phase.get("total", 0),
                "valid_checksum": phase.get("valid_checksum"),
                "confidence":    phase.get("confidence", 0),
                "errors":        phase.get("errors", []),
                "message":       phase.get("error"),
            }

            update_search_query(cur, query_id, db_status)

        # ----------------------------------------------------
        # Branch 2 — Barcode
        # EAN-13 checksum -> OpenFoodFacts -> GS1 fallback -> ABR
        # ----------------------------------------------------
        elif input_type == "barcode":
            phase = run_barcode_phase(
                barcode,
                abr_lookup_fn=search_company_name_with_abr,
            )
            pipeline_steps = phase.get("pipeline", [])
            db_status      = "resolved" if phase["success"] else "failed"

            result = {
                "input_type":       "barcode",
                "input_value":      barcode,
                "status":           phase.get("status", "not_found"),
                "source":           phase.get("source"),
                "product":          phase.get("product"),
                "brand_raw":        phase.get("brand_raw"),
                "brand_clean":      phase.get("brand_clean"),
                "brand_owner":      phase.get("brand_owner"),
                "manufacturer":     phase.get("manufacturer"),
                "abn_verification": phase.get("abr"),
                "confidence":       phase.get("confidence", 0),
                "errors":           phase.get("errors", []),
                "message":          phase.get("error"),
            }

            update_search_query(cur, query_id, db_status)

        # ----------------------------------------------------
        # Branch 3 — Brand name
        # IP Australia Trade Mark Search -> extract owner -> ABR
        # ----------------------------------------------------
        elif input_type == "brand_name":
            phase = run_brand_phase(
                brand,
                abr_lookup_fn=search_company_name_with_abr,
            )
            pipeline_steps = phase.get("pipeline", [])
            db_status      = "resolved" if phase["success"] else "failed"

            result = {
                "input_type":       "brand",
                "input_value":      brand,
                "status":           phase.get("status", "not_found"),
                "source":           "IP Australia Trade Mark + ABR",
                "brand_name":       brand,
                "trademark":        phase.get("trademark"),
                "legal_owner":      phase.get("legal_owner"),
                "abn_verification": phase.get("abr"),
                "confidence":       phase.get("confidence", 0),
                "errors":           phase.get("errors", []),
                "message":          phase.get("error"),
            }

            update_search_query(cur, query_id, db_status)

        conn.commit()

        return {
            "query_id":          str(query_id),
            "status":            "success",
            "input_type":        frontend_type,
            "input_value":       input_value,
            "resolution_status": db_status,
            "pipeline_steps":    pipeline_steps,
            "result":            result,
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ============================================================
# Standalone Test / Diagnostic Endpoints
# ============================================================

@app.get("/api/abn/verify/{abn}")
def verify_abn_endpoint(abn: str):
    """
    Verify a single ABN (format + checksum + ABR).
    Example: GET /api/abn/verify/88000014675
    """
    from abn_pipeline import validate_abn_checksum, run_abn_phase
    cleaned = clean_abn(abn)
    if not is_abn(cleaned):
        raise HTTPException(status_code=400, detail="ABN must be 11 digits")
    return run_abn_phase(cleaned)


@app.get("/api/company/search/{company_name}")
def lookup_company_name(company_name: str):
    """
    Search a company name via ABR.
    Example: GET /api/company/search/Coles
    """
    from abn_pipeline import run_company_phase
    cleaned = clean_text(company_name)
    if not cleaned or len(cleaned) < 2:
        raise HTTPException(status_code=400, detail="Company name must be at least 2 characters")
    return run_company_phase(cleaned)


@app.get("/api/barcode/{barcode}")
def lookup_barcode(barcode: str):
    """
    Run the full barcode pipeline (EAN-13 -> OpenFoodFacts -> GS1 -> ABR).
    Example: GET /api/barcode/9310055002440
    """
    return run_barcode_phase(barcode, abr_lookup_fn=search_company_name_with_abr)


@app.get("/api/trademark/token-test")
def test_ip_australia_token():
    """Verify IP Australia OAuth credentials are configured correctly."""
    token = get_ip_australia_access_token()
    if not token:
        return {
            "status":  "error",
            "message": "Unable to obtain token — check IP_AUSTRALIA_CLIENT_ID "
                       "and IP_AUSTRALIA_CLIENT_SECRET in .env",
        }
    return {"status": "success", "token_preview": token[:20] + "..."}


@app.get("/api/trademark/search/{brand}")
def lookup_trademark(brand: str):
    """
    Run the full brand pipeline (IP Australia Trade Mark -> ABR).
    Example: GET /api/trademark/search/TimTam
    """
    cleaned = clean_text(brand)
    if not cleaned or len(cleaned) < 2:
        raise HTTPException(status_code=400, detail="Brand must be at least 2 characters")
    return run_brand_phase(cleaned, abr_lookup_fn=search_company_name_with_abr)


# ============================================================
# Search History
# ============================================================

@app.get("/api/search/history/{user_id}")
def get_search_history(user_id: str):
    """Return search history for a given user_id, newest first."""
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute(
            """
            SELECT query_id, input_type, input_value, resolution_status,
                   resolved_company_id, resolved_brand_id, resolved_product_id, submitted_at
            FROM search_query
            WHERE user_id = %s
            ORDER BY submitted_at DESC;
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        return {"user_id": user_id, "history": [serialize_row(r) for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


@app.get("/api/search/query/{query_id}")
def get_search_query(query_id: str):
    """Retrieve a single search_query record by query_id."""
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute(
            """
            SELECT query_id, user_id, input_type, input_value, resolution_status,
                   resolved_company_id, resolved_brand_id, resolved_product_id, submitted_at
            FROM search_query
            WHERE query_id = %s;
            """,
            (query_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Query not found")
        return {"query": serialize_row(row)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()
