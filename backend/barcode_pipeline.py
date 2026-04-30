"""
barcode_pipeline.py
===================

Purpose
-------
Handles the complete barcode input phase of the EcoTrace search pipeline.

Barcode pipeline flow
---------------------
  User input (EAN-13 barcode)
      │
      ├─── 1. Validate      ──── EAN-13 format + checksum digit check
      │
      ├─── 2. Lookup        ──── OpenFoodFacts  (only source; no GS1 fallback)
      │                           returns product_name, brand_raw, brand_owner,
      │                           manufacturer, categories, image_url
      │
      ├─── 3. TM Resolution ──── IP Australia Trade Mark Search
      │                           brand token → legal owner name
      │                           (reuses brand_pipeline OAuth token cache)
      │
      └─── 4. ABR Verify    ──── ABR name search using legal owner
                                  → ABN, entity type, GST status

External APIs
-------------
- OpenFoodFacts  https://world.openfoodfacts.org/api/v2/product/{barcode}.json
  No key required.  Must send a User-Agent header or requests are rejected.

- IP Australia Trade Mark Search  (OAuth token managed in brand_pipeline)
  Required .env: IP_AUSTRALIA_CLIENT_ID, IP_AUSTRALIA_CLIENT_SECRET
  Optional .env: IP_AUSTRALIA_TOKEN_URL, IP_AUSTRALIA_TRADEMARK_URL

- ABR Name Search  (passed in as abr_lookup_fn to avoid circular imports)
  Required .env: ABR_GUID

Usage
-----
From main.py barcode branch:

    from barcode_pipeline import run_barcode_phase
    from abn_pipeline   import search_company_name_with_abr

    result = run_barcode_phase(barcode, abr_lookup_fn=search_company_name_with_abr)

Result keys
-----------
    success        bool
    barcode        str   cleaned 13-digit barcode
    product        dict  product_name, image_url, categories, countries
    brand_raw      str   raw brand string from OpenFoodFacts
    brand_clean    str   normalised single brand token
    brand_owner    str   legal entity name from OpenFoodFacts (if present)
    manufacturer   str   manufacturing location or None
    trademark      dict  best IP Australia trademark match (or None)
    legal_owner    str   owner extracted from trademark record (or None)
    abr            dict  ABR result dict (or None)
    source         str   always "OpenFoodFacts"
    confidence     int   0-100
    pipeline       list  ordered steps executed
    errors         list  non-fatal error messages
"""

import re
from typing import Any, Dict, List, Optional

import requests

# Import IP Australia trademark helpers from brand_pipeline.
# This reuses the shared OAuth token cache so both pipelines share one token.
from brand_pipeline import (
    _parse_trademark_results,
    _search_trademarks,
    _strip_legal_suffix,
)


# ============================================================
# Constants
# ============================================================

_OFF_USER_AGENT = "EcoTrace-App/1.0 (student project; contact via GitHub)"
_DEFAULT_TIMEOUT = 20


# ============================================================
# Section 1 — EAN-13 Validation
# ============================================================

def validate_ean13(barcode: str) -> Dict[str, Any]:
    """
    Validate that a string is a well-formed EAN-13 barcode.

    Checks
    ------
    1. Digits only (spaces / dashes stripped first).
    2. Exactly 13 digits.
    3. Check digit correct per GS1 checksum algorithm.

    EAN-13 checksum
    ---------------
    Multiply each of the first 12 digits alternately by 1 (odd positions)
    and 3 (even positions) counting from the left at position 1.
    Sum the 12 weighted values.
    Check digit = (10 - (total % 10)) % 10

    Returns dict with:
        valid   bool
        digits  str   cleaned 13-digit string (or raw input on failure)
        error   str   human-readable reason (omitted on success)
    """
    cleaned = re.sub(r"[\s\-]", "", barcode or "")

    if not cleaned.isdigit():
        return {"valid": False, "digits": barcode,
                "error": "Barcode must contain digits only."}

    if len(cleaned) != 13:
        return {"valid": False, "digits": cleaned,
                "error": f"Expected 13 digits for EAN-13, got {len(cleaned)}."}

    weights = [1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3]
    total = sum(int(cleaned[i]) * weights[i] for i in range(12))
    expected_check = (10 - (total % 10)) % 10
    actual_check   = int(cleaned[12])

    if expected_check != actual_check:
        return {
            "valid":  False,
            "digits": cleaned,
            "error":  (
                f"Check digit mismatch: expected {expected_check}, "
                f"got {actual_check}. Barcode may have been mistyped."
            ),
        }

    return {"valid": True, "digits": cleaned}


# ============================================================
# Section 2 — OpenFoodFacts Lookup  (only product source)
# ============================================================

def _lookup_openfoodfacts(barcode: str) -> Dict[str, Any]:
    """
    Query the OpenFoodFacts v2 product API.

    Requests only the fields needed by this pipeline to keep the payload
    small.  OpenFoodFacts returns status=1 when the product is found.

    Returns dict with success, source, and product fields.
    """
    url = f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
    params = {
        "fields": (
            "product_name,brands,brand_owner,manufacturing_places,"
            "categories,image_url,countries"
        ),
    }
    headers = {
        "User-Agent": _OFF_USER_AGENT,
        "Accept":     "application/json",
    }

    try:
        response = requests.get(
            url, params=params, headers=headers, timeout=_DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()

        if data.get("status") != 1:
            return {
                "success": False,
                "source":  "OpenFoodFacts",
                "message": "Product not found in OpenFoodFacts database.",
            }

        product = data.get("product", {})
        return {
            "success":      True,
            "source":       "OpenFoodFacts",
            "product_name": product.get("product_name") or None,
            "brand_raw":    product.get("brands")       or None,
            "brand_owner":  product.get("brand_owner")  or None,
            "manufacturer": product.get("manufacturing_places") or None,
            "categories":   product.get("categories")   or None,
            "image_url":    product.get("image_url")    or None,
            "countries":    product.get("countries")    or None,
        }

    except requests.exceptions.Timeout:
        return {"success": False, "source": "OpenFoodFacts",
                "message": "OpenFoodFacts request timed out."}
    except requests.exceptions.RequestException as exc:
        return {"success": False, "source": "OpenFoodFacts",
                "message": f"OpenFoodFacts network error: {exc}"}
    except Exception as exc:
        return {"success": False, "source": "OpenFoodFacts",
                "message": f"OpenFoodFacts unexpected error: {exc}"}


# ============================================================
# Section 3 — Brand Normalisation
# ============================================================

def extract_best_brand(
    brand_raw:   Optional[str],
    brand_owner: Optional[str],
) -> Optional[str]:
    """
    Return the single best brand token for downstream trademark lookup.

    Priority
    --------
    1. brand_owner  (legal entity name from OpenFoodFacts)
    2. First token from brand_raw  (comma-separated brands string)

    Normalisation
    -------------
    - Strip whitespace.
    - Remove common legal suffixes (Pty Ltd, Ltd, Inc, Corp, LLC, Co).
    - Collapse multiple whitespace to a single space.
    """
    candidate = brand_owner or brand_raw
    if not candidate:
        return None

    if candidate == brand_raw and "," in candidate:
        candidate = candidate.split(",")[0]

    candidate = candidate.strip()

    legal_suffixes = [
        r"\bPty\.?\s*Ltd\.?",
        r"\bPty\.?",
        r"\bLtd\.?",
        r"\bInc\.?",
        r"\bCorp\.?",
        r"\bLLC\.?",
        r"\bCo\.?",
        r"\bAustralia\b",
    ]
    for pattern in legal_suffixes:
        candidate = re.sub(pattern, "", candidate, flags=re.IGNORECASE)

    candidate = re.sub(r"\s+", " ", candidate).strip()
    return candidate if candidate else None


# ============================================================
# Section 4 — IP Australia Trademark Resolution
# ============================================================

def _resolve_trademark_owner(brand_token: str) -> Dict[str, Any]:
    """
    Run an IP Australia Trade Mark quick search for *brand_token* and
    return the best-ranked legal owner name.

    This is a thin wrapper around the shared helpers imported from
    brand_pipeline.  The OAuth token cache in brand_pipeline is reused,
    so both the brand and barcode pipelines share a single token.

    Returns dict with:
        success      bool
        legal_owner  str | None
        trademark    dict | None  (best trademark summary)
        candidates   int
        errors       list
    """
    errors: List[str] = []

    # --- Trademark API call ---
    tm_response = _search_trademarks(brand_token)
    if not tm_response.get("success"):
        msg = tm_response.get("message", "IP Australia trademark search failed")
        errors.append(msg)
        return {
            "success":     False,
            "legal_owner": None,
            "trademark":   None,
            "candidates":  0,
            "errors":      errors,
        }

    # --- Parse + rank ---
    parsed = _parse_trademark_results(tm_response["data"], brand_token)

    if not parsed["found"]:
        errors.append("No trademark records matched the brand token.")

    return {
        "success":     parsed["found"],
        "legal_owner": parsed.get("legal_owner"),
        "trademark":   parsed.get("trademark"),
        "candidates":  parsed.get("candidates", 0),
        "errors":      errors,
    }


# ============================================================
# Section 5 — Orchestrated Barcode Resolution
# ============================================================

def resolve_barcode(
    barcode:       str,
    abr_lookup_fn: Any = None,
) -> Dict[str, Any]:
    """
    Full barcode resolution pipeline.

    Steps
    -----
    1. Validate EAN-13 format and checksum.
    2. Query OpenFoodFacts — the sole product data source.
       If it fails, return not-found immediately (no GS1 fallback).
    3. Normalise the best brand token for trademark search.
    4. IP Australia Trade Mark Search → extract legal owner.
    5. ABR name lookup using legal_owner (or brand_owner / brand_clean).

    Parameters
    ----------
    barcode       : str  — raw barcode string from user input.
    abr_lookup_fn : callable, optional
        Accepts a company/brand name string, returns an ABR result dict.
        Pass `search_company_name_with_abr` from abn_pipeline.
        If None, the ABR step is skipped.
    """
    pipeline: List[str] = []
    errors:   List[str] = []

    # ----------------------------------------------------------
    # Step 1 — Validate barcode
    # ----------------------------------------------------------
    pipeline.append("EAN-13 validation")
    validation = validate_ean13(barcode)

    if not validation["valid"]:
        return {
            "success":    False,
            "barcode":    barcode,
            "error":      validation["error"],
            "pipeline":   pipeline,
            "errors":     errors,
            "confidence": 0,
        }

    clean_barcode = validation["digits"]

    # ----------------------------------------------------------
    # Step 2 — OpenFoodFacts lookup  (only source — no GS1 fallback)
    # ----------------------------------------------------------
    pipeline.append("OpenFoodFacts product lookup")
    off_result = _lookup_openfoodfacts(clean_barcode)

    if not off_result["success"]:
        errors.append(f"OpenFoodFacts: {off_result.get('message', 'unknown error')}")
        return {
            "success":    False,
            "barcode":    clean_barcode,
            "error":      off_result.get("message", "Product not found in OpenFoodFacts."),
            "pipeline":   pipeline,
            "errors":     errors,
            "confidence": 0,
        }

    brand_raw    = off_result.get("brand_raw")
    brand_owner  = off_result.get("brand_owner")
    manufacturer = off_result.get("manufacturer")

    # ----------------------------------------------------------
    # Step 3 — Normalise brand token
    # ----------------------------------------------------------
    pipeline.append("Brand extraction and normalisation")
    brand_clean = extract_best_brand(brand_raw, brand_owner)

    # ----------------------------------------------------------
    # Step 4 — IP Australia Trade Mark resolution
    # ----------------------------------------------------------
    tm_result:    Optional[Dict[str, Any]] = None
    legal_owner:  Optional[str]            = None

    # Prefer brand_clean for trademark search (legal suffixes stripped).
    # Fall back to brand_owner if no clean token available.
    tm_search_token = brand_clean or brand_owner

    if tm_search_token:
        pipeline.append(f"IP Australia trademark search: '{tm_search_token}'")
        tm_result = _resolve_trademark_owner(tm_search_token)

        if tm_result.get("errors"):
            errors.extend(tm_result["errors"])

        legal_owner = tm_result.get("legal_owner") if tm_result else None

        # If trademark search returned no owner, try stripping legal suffixes
        # from brand_owner and retrying (mirrors brand_pipeline retry logic).
        if not legal_owner and brand_owner:
            short = _strip_legal_suffix(brand_owner)
            if short and short != tm_search_token:
                pipeline.append(f"IP Australia trademark retry: '{short}'")
                tm_retry = _resolve_trademark_owner(short)
                if tm_retry.get("legal_owner"):
                    tm_result   = tm_retry
                    legal_owner = tm_retry["legal_owner"]

    # ----------------------------------------------------------
    # Step 5 — ABR name lookup
    # ----------------------------------------------------------
    abr_result: Optional[Dict[str, Any]] = None

    # Priority for ABR search term:
    #   1. legal_owner from trademark  (most authoritative)
    #   2. brand_owner from OpenFoodFacts
    #   3. brand_clean (normalised trading name)
    abr_search_term = legal_owner or brand_owner or brand_clean

    if abr_lookup_fn and abr_search_term:
        pipeline.append(f"ABR name lookup: '{abr_search_term}'")
        abr_result = abr_lookup_fn(abr_search_term)

        # Retry with brand_clean if the primary term fails.
        if (
            not abr_result.get("success")
            and brand_clean
            and brand_clean != abr_search_term
        ):
            pipeline.append(f"ABR name lookup retry: '{brand_clean}'")
            abr_result = abr_lookup_fn(brand_clean)

    # ----------------------------------------------------------
    # Confidence scoring
    # ----------------------------------------------------------
    confidence = 0
    confidence += 40  # OpenFoodFacts product found (we're past that check now)
    if brand_raw:
        confidence += 10
    if brand_owner:
        confidence += 5
    if brand_clean:
        confidence += 5
    if tm_result and tm_result.get("success"):
        tm_status = str(
            (tm_result.get("trademark") or {}).get("status") or ""
        ).lower()
        confidence += 20 if tm_status == "registered" else 10
    if legal_owner:
        confidence += 10
    if abr_result and abr_result.get("success"):
        confidence += 10

    # ----------------------------------------------------------
    # Unified result
    # ----------------------------------------------------------
    return {
        "success":  True,
        "barcode":  clean_barcode,
        "product": {
            "product_name": off_result.get("product_name"),
            "image_url":    off_result.get("image_url"),
            "categories":   off_result.get("categories"),
            "countries":    off_result.get("countries"),
        },
        "brand_raw":    brand_raw,
        "brand_clean":  brand_clean,
        "brand_owner":  brand_owner,
        "manufacturer": manufacturer,
        "trademark":    tm_result.get("trademark") if tm_result else None,
        "legal_owner":  legal_owner,
        "abr":          abr_result,
        "source":       "OpenFoodFacts",
        "confidence":   min(confidence, 100),
        "pipeline":     pipeline,
        "errors":       errors,
    }


# ============================================================
# Section 6 — Convenience wrapper for main.py
# ============================================================

def run_barcode_phase(
    barcode:       str,
    abr_lookup_fn: Any = None,
) -> Dict[str, Any]:
    """
    Thin wrapper used by the /api/search barcode branch in main.py.

    Usage in main.py
    ----------------
        from barcode_pipeline import run_barcode_phase
        from abn_pipeline     import search_company_name_with_abr

        barcode_result = run_barcode_phase(
            barcode,
            abr_lookup_fn=search_company_name_with_abr,
        )
        db_status = "resolved" if barcode_result["success"] else "failed"

        result = {
            "input_type":       "barcode",
            "input_value":      barcode,
            "status":           barcode_result["status"],
            "source":           barcode_result.get("source"),
            "product":          barcode_result.get("product"),
            "brand_raw":        barcode_result.get("brand_raw"),
            "brand_clean":      barcode_result.get("brand_clean"),
            "brand_owner":      barcode_result.get("brand_owner"),
            "trademark":        barcode_result.get("trademark"),
            "legal_owner":      barcode_result.get("legal_owner"),
            "manufacturer":     barcode_result.get("manufacturer"),
            "abn_verification": barcode_result.get("abr"),
            "confidence":       barcode_result.get("confidence", 0),
            "pipeline_steps":   barcode_result.get("pipeline", []),
            "errors":           barcode_result.get("errors", []),
        }
    """
    resolved = resolve_barcode(barcode, abr_lookup_fn=abr_lookup_fn)
    resolved["status"] = "external_resolved" if resolved["success"] else "not_found"
    return resolved
