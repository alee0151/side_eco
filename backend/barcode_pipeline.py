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
      │                            returns product_name, brand_raw, brand_owner,
      │                            manufacturer, categories, image_url
      │
      ├─── 3. TM Resolution ──── IP Australia Trade Mark Search
      │                            brand token → legal owner name
      │                            (reuses brand_pipeline OAuth token cache)
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
"""

import re
from typing import Any, Dict, List, Optional

import requests

# Reuse the shared OAuth token cache and trademark helpers from brand_pipeline.
from brand_pipeline import (
    _quick_search,
    _fetch_trademark_detail,
    _extract_owner_from_record,
    _build_trademark_summary,
    _strip_legal_suffix,
)


# ============================================================
# Constants
# ============================================================

_OFF_USER_AGENT  = "EcoTrace-App/1.0 (student project; contact via GitHub)"
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
    """
    cleaned = re.sub(r"[\s\-]", "", barcode or "")

    if not cleaned.isdigit():
        return {"valid": False, "digits": barcode,
                "error": "Barcode must contain digits only."}

    if len(cleaned) != 13:
        return {"valid": False, "digits": cleaned,
                "error": f"Expected 13 digits for EAN-13, got {len(cleaned)}."}

    weights = [1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3]
    total   = sum(int(cleaned[i]) * weights[i] for i in range(12))
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
# Section 2 — OpenFoodFacts Lookup
# ============================================================

def _lookup_openfoodfacts(barcode: str) -> Dict[str, Any]:
    """
    Query the OpenFoodFacts v2 product API.
    Returns success, source, and product fields.
    """
    url    = f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
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

    Priority: brand_owner > first token of brand_raw.
    Strips legal suffixes and collapses whitespace.
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
    Run an IP Australia Trade Mark quick search (WORD + REGISTERED) for
    *brand_token* and return the best-ranked legal owner name.

    Calls the shared helpers imported from brand_pipeline so both
    pipelines share the same OAuth token cache.
    """
    errors: List[str] = []

    # Step A — quick search (WORD + REGISTERED)
    search_resp = _quick_search(brand_token)
    if not search_resp.get("success"):
        msg = search_resp.get("message", "IP Australia trademark search failed")
        errors.append(msg)
        return {
            "success":     False,
            "legal_owner": None,
            "trademark":   None,
            "candidates":  0,
            "errors":      errors,
        }

    search_data = search_resp["data"]
    ids         = search_data.get("trademarkIds") or []

    if not ids:
        return {
            "success":     False,
            "legal_owner": None,
            "trademark":   None,
            "candidates":  0,
            "errors":      ["No registered word-mark trademarks found."],
        }

    # Step B — fetch detail for the first (highest-relevance) ID
    best_id     = str(ids[0])
    detail_resp = _fetch_trademark_detail(best_id)

    if not detail_resp.get("success"):
        msg = detail_resp.get("message", f"Could not fetch trademark {best_id}")
        errors.append(msg)
        return {
            "success":     False,
            "legal_owner": None,
            "trademark":   {"number": best_id},
            "candidates":  len(ids),
            "errors":      errors,
        }

    record      = detail_resp["data"]
    legal_owner = _extract_owner_from_record(record)
    trademark   = _build_trademark_summary(record, legal_owner)

    return {
        "success":     True,
        "legal_owner": legal_owner,
        "trademark":   trademark,
        "candidates":  len(ids),
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
    2. Query OpenFoodFacts.
    3. Normalise brand token.
    4. IP Australia Trade Mark Search (WORD + REGISTERED) → legal owner.
    5. ABR name lookup.
    """
    pipeline: List[str] = []
    errors:   List[str] = []

    # Step 1 — Validate
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

    # Step 2 — OpenFoodFacts
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

    # Step 3 — Normalise brand token
    pipeline.append("Brand extraction and normalisation")
    brand_clean = extract_best_brand(brand_raw, brand_owner)

    # Step 4 — IP Australia Trademark
    tm_result:   Optional[Dict[str, Any]] = None
    legal_owner: Optional[str]            = None
    tm_search_token = brand_clean or brand_owner

    if tm_search_token:
        pipeline.append(f"IP Australia trademark search: '{tm_search_token}'")
        tm_result = _resolve_trademark_owner(tm_search_token)

        if tm_result.get("errors"):
            errors.extend(tm_result["errors"])

        legal_owner = tm_result.get("legal_owner") if tm_result else None

        # Retry with suffix-stripped brand_owner if initial search found nothing
        if not legal_owner and brand_owner:
            short = _strip_legal_suffix(brand_owner)
            if short and short != tm_search_token:
                pipeline.append(f"IP Australia trademark retry: '{short}'")
                tm_retry = _resolve_trademark_owner(short)
                if tm_retry.get("legal_owner"):
                    tm_result   = tm_retry
                    legal_owner = tm_retry["legal_owner"]

    # Step 5 — ABR
    abr_result:     Optional[Dict[str, Any]] = None
    abr_search_term = legal_owner or brand_owner or brand_clean

    if abr_lookup_fn and abr_search_term:
        pipeline.append(f"ABR name lookup: '{abr_search_term}'")
        abr_result = abr_lookup_fn(abr_search_term)

        if not abr_result.get("success") and brand_clean and brand_clean != abr_search_term:
            pipeline.append(f"ABR name lookup retry: '{brand_clean}'")
            abr_result = abr_lookup_fn(brand_clean)

    # Confidence
    confidence  = 40  # OpenFoodFacts hit
    confidence += 10 if brand_raw   else 0
    confidence += 5  if brand_owner else 0
    confidence += 5  if brand_clean else 0
    if tm_result and tm_result.get("success"):
        tm_status  = str((tm_result.get("trademark") or {}).get("status") or "").lower()
        confidence += 20 if tm_status == "registered" else 10
    confidence += 10 if legal_owner                              else 0
    confidence += 10 if abr_result and abr_result.get("success") else 0

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
    """Thin wrapper used by the /api/search barcode branch in main.py."""
    resolved = resolve_barcode(barcode, abr_lookup_fn=abr_lookup_fn)
    resolved["status"] = "external_resolved" if resolved["success"] else "not_found"
    return resolved
