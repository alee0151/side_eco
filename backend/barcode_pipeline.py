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
      ├─── 1. Validate  ──── EAN-13 format + checksum digit check
      │
      ├─── 2. Lookup    ──── OpenFoodFacts  (primary, free, no key required)
      │                └─── GS1 Verified-by-GS1  (fallback, free public API)
      │
      ├─── 3. Extract   ──── brand name, manufacturer, product name, image
      │
      └─── 4. Verify    ──── ABR name search using extracted brand/manufacturer

External APIs
-------------
- OpenFoodFacts  https://world.openfoodfacts.org/api/v2/product/{barcode}.json
  No key required.  Must send a User-Agent header or requests are rejected.

- GS1 Verified by GS1  https://www.gs1.org/services/verified-by-gs1/results
  Free public lookup.  Accepts 14-digit GTIN (EAN-13 padded with one leading 0).
  Returns the registered company name and brand.

- ABR Name Search  (via search_company_name_with_abr in main.py)
  Required .env: ABR_GUID

Usage
-----
From main.py barcode branch:

    from barcode_pipeline import resolve_barcode

    result = resolve_barcode(barcode_string)
    # result["success"]       bool
    # result["product"]       product-level fields
    # result["brand_raw"]     raw brand string from API
    # result["brand_clean"]   first normalised brand token
    # result["manufacturer"]  manufacturer string or None
    # result["abr"]           ABR verification dict or None
    # result["source"]        which API supplied the product data
    # result["confidence"]    int 0-100
    # result["errors"]        list of non-fatal error messages
"""

import os
import re
from typing import Any, Dict, List, Optional

import requests


# ============================================================
# Constants
# ============================================================

# OpenFoodFacts requires a descriptive User-Agent; anonymous requests are
# rate-limited and may be rejected.
_OFF_USER_AGENT = "EcoTrace-App/1.0 (student project; contact via GitHub)"

# GS1 Verified-by-GS1 endpoint.  Accepts 14-digit GTIN.
# EAN-13 barcodes are padded to 14 digits with one leading zero.
_GS1_VBG_URL = "https://www.gs1.org/services/verified-by-gs1/results"

# Timeout in seconds.  Government and GS1 endpoints can be slow.
_DEFAULT_TIMEOUT = 20


# ============================================================
# Section 1 — Validation
# ============================================================

def validate_ean13(barcode: str) -> Dict[str, Any]:
    """
    Validate that a string is a well-formed EAN-13 barcode.

    Checks performed
    ----------------
    1. Numeric digits only (spaces and dashes are stripped first).
    2. Exactly 13 digits.
    3. Check digit (13th digit) is correct per the GS1 checksum algorithm.

    EAN-13 checksum algorithm
    -------------------------
    - Multiply each of the first 12 digits alternately by 1 (odd positions)
      and 3 (even positions), counting from the left starting at position 1.
    - Sum all 12 weighted values.
    - Check digit = (10 - (total_sum % 10)) % 10
    - The result must equal the 13th digit in the barcode.

    Returns
    -------
    dict with:
        valid   bool
        digits  str   cleaned 13-digit string (or raw input on failure)
        error   str   human-readable reason on failure, omitted on success
    """
    # Strip common non-digit separators entered by users (spaces, dashes).
    cleaned = re.sub(r"[\s\-]", "", barcode or "")

    if not cleaned.isdigit():
        return {
            "valid": False,
            "digits": barcode,
            "error": "Barcode must contain digits only.",
        }

    if len(cleaned) != 13:
        return {
            "valid": False,
            "digits": cleaned,
            "error": f"Expected 13 digits for EAN-13, got {len(cleaned)}.",
        }

    # --- EAN-13 checksum ---
    # Weights alternate: 1 for odd positions, 3 for even positions (1-indexed).
    weights = [1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3]  # 12 weights for first 12 digits
    total = sum(int(cleaned[i]) * weights[i] for i in range(12))
    expected_check = (10 - (total % 10)) % 10
    actual_check = int(cleaned[12])

    if expected_check != actual_check:
        return {
            "valid": False,
            "digits": cleaned,
            "error": (
                f"Check digit mismatch: expected {expected_check}, "
                f"got {actual_check}. Barcode may have been mistyped."
            ),
        }

    return {"valid": True, "digits": cleaned}


def ean13_to_gtin14(ean13: str) -> str:
    """
    Pad a 13-digit EAN-13 to a 14-digit GTIN by prepending a leading zero.

    GS1 Verified-by-GS1 and some other GS1 services require GTIN-14 format.
    """
    return "0" + ean13


# ============================================================
# Section 2 — OpenFoodFacts Lookup  (primary data source)
# ============================================================

def _lookup_openfoodfacts(barcode: str) -> Dict[str, Any]:
    """
    Query the OpenFoodFacts v2 product API.

    API documentation: https://openfoodfacts.github.io/openfoodfacts-server/api/

    Requested fields are limited to what the EcoTrace pipeline needs:
    product_name, brands, brand_owner, manufacturing_places, categories,
    image_url.  Requesting only these fields reduces response payload size.

    Returns
    -------
    dict with success, source, and product fields — or success=False with message.
    """
    url = f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
    params = {
        # Request only the fields we need to keep the payload small.
        "fields": "product_name,brands,brand_owner,manufacturing_places,categories,image_url,countries",
    }
    headers = {
        "User-Agent": _OFF_USER_AGENT,
        "Accept": "application/json",
    }

    try:
        response = requests.get(
            url, params=params, headers=headers, timeout=_DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()

        # OpenFoodFacts returns status=1 for found, status=0 for not found.
        if data.get("status") != 1:
            return {
                "success": False,
                "source": "OpenFoodFacts",
                "message": "Product not found in OpenFoodFacts database.",
            }

        product = data.get("product", {})

        return {
            "success": True,
            "source": "OpenFoodFacts",
            "product_name":  product.get("product_name") or None,
            # brands is a comma-separated string: "Tim Tam, Arnott's"
            "brand_raw":     product.get("brands") or None,
            # brand_owner is the legal entity registered as the brand owner.
            # This is more reliable for ABR lookup than brands.
            "brand_owner":   product.get("brand_owner") or None,
            "manufacturer":  product.get("manufacturing_places") or None,
            "categories":    product.get("categories") or None,
            "image_url":     product.get("image_url") or None,
            "countries":     product.get("countries") or None,
        }

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "source": "OpenFoodFacts",
            "message": "OpenFoodFacts request timed out.",
        }
    except requests.exceptions.RequestException as e:
        return {
            "success": False,
            "source": "OpenFoodFacts",
            "message": f"OpenFoodFacts network error: {str(e)}",
        }
    except Exception as e:
        return {
            "success": False,
            "source": "OpenFoodFacts",
            "message": f"OpenFoodFacts unexpected error: {str(e)}",
        }


# ============================================================
# Section 3 — GS1 Verified-by-GS1 Lookup  (fallback)
# ============================================================

def _lookup_gs1_verified(barcode: str) -> Dict[str, Any]:
    """
    Query the GS1 Verified-by-GS1 public API using a 14-digit GTIN.

    This is a free public endpoint — no API key required.
    It returns the GS1-registered company name and product description
    as declared by the brand owner in the GS1 registry.

    API reference: https://www.gs1.org/services/verified-by-gs1

    GS1 returns a JSON envelope like:
    {
      "totalRecordsCount": 1,
      "currentPage": 1,
      "products": [
        {
          "gtin": "00123456789012",
          "gs1LicenseeGLN": "...",
          "gs1LicenceeName": "Example Corp Pty Ltd",
          "brandName": "ExBrand",
          "productDescription": "Example Product",
          ...
        }
      ]
    }

    Returns
    -------
    dict with success, source, and product fields — or success=False with message.
    """
    gtin14 = ean13_to_gtin14(barcode)

    params = {"gtin": gtin14}
    headers = {
        "Accept": "application/json",
        "User-Agent": _OFF_USER_AGENT,
    }

    try:
        response = requests.get(
            _GS1_VBG_URL, params=params, headers=headers, timeout=_DEFAULT_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()

        products = data.get("products", [])
        if not products:
            return {
                "success": False,
                "source": "GS1 Verified-by-GS1",
                "message": "Product not found in GS1 registry.",
            }

        # Use the first registered product record.
        product = products[0]

        return {
            "success": True,
            "source": "GS1 Verified-by-GS1",
            "product_name":    product.get("productDescription") or None,
            # gs1LicenceeName is the GS1-registered legal company name.
            # This is the most authoritative value for ABR lookup.
            "brand_raw":       product.get("brandName") or None,
            "brand_owner":     product.get("gs1LicenceeName") or None,
            "manufacturer":    None,  # GS1 VbG does not expose manufacturing location
            "categories":      None,
            "image_url":       None,
            "countries":       None,
            # Keep the full GS1 licensee name for ABR matching.
            "gs1_licensee":    product.get("gs1LicenceeName") or None,
            "gs1_licensee_gln": product.get("gs1LicenseeGLN") or None,
        }

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "source": "GS1 Verified-by-GS1",
            "message": "GS1 request timed out.",
        }
    except requests.exceptions.RequestException as e:
        return {
            "success": False,
            "source": "GS1 Verified-by-GS1",
            "message": f"GS1 network error: {str(e)}",
        }
    except Exception as e:
        return {
            "success": False,
            "source": "GS1 Verified-by-GS1",
            "message": f"GS1 unexpected error: {str(e)}",
        }


# ============================================================
# Section 4 — Brand Normalisation
# ============================================================

def extract_best_brand(brand_raw: Optional[str], brand_owner: Optional[str]) -> Optional[str]:
    """
    Return the single best brand token to use for downstream trademark lookup.

    Priority
    --------
    1. brand_owner  (legal entity name registered with GS1 or OpenFoodFacts)
    2. First token from brand_raw  (comma-separated brands string)

    Normalisation applied
    ---------------------
    - Strip leading/trailing whitespace.
    - Remove common legal suffixes that break trademark search:
      'Pty Ltd', 'Pty. Ltd.', 'Ltd', 'Inc', 'Corp', 'Co.', 'LLC'
      These suffixes are removed because IP Australia trademark search works
      better with trading names (e.g. "Arnott's") than legal names
      ("Arnott's Biscuits Holdings Pty Ltd").
    - Collapse multiple whitespace to single space.
    """
    # Prefer brand_owner as it's more stable for ABR matching later.
    candidate = brand_owner or brand_raw
    if not candidate:
        return None

    # If brand_raw has multiple brands, take only the first one.
    # e.g. "Tim Tam, Arnott's, Campbell's" -> "Tim Tam"
    if candidate == brand_raw and "," in candidate:
        candidate = candidate.split(",")[0]

    candidate = candidate.strip()

    # Remove legal entity suffixes to improve trademark search recall.
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

    # Collapse extra whitespace left after suffix removal.
    candidate = re.sub(r"\s+", " ", candidate).strip()

    return candidate if candidate else None


# ============================================================
# Section 5 — Orchestrated Barcode Resolution
# ============================================================

def resolve_barcode(
    barcode: str,
    abr_lookup_fn=None,
) -> Dict[str, Any]:
    """
    Full barcode resolution pipeline.

    Steps
    -----
    1. Validate EAN-13 format and checksum.
    2. Query OpenFoodFacts (primary).
    3. If OpenFoodFacts fails, query GS1 Verified-by-GS1 (fallback).
    4. Extract and normalise the best brand name.
    5. Optionally run ABR name lookup on the extracted brand.

    Parameters
    ----------
    barcode       : str
        Raw barcode string from user input.

    abr_lookup_fn : callable, optional
        A function that accepts a company/brand name string and returns an ABR
        result dict.  Pass `search_company_name_with_abr` from main.py.
        If None, the ABR step is skipped.

    Returns
    -------
    dict with the following keys:

        success      bool
        barcode      str   cleaned 13-digit barcode
        product      dict  product_name, image_url, categories, countries
        brand_raw    str   raw brand string from the product API
        brand_clean  str   normalised single brand token for downstream use
        brand_owner  str   legal entity name from API (best for ABR lookup)
        manufacturer str   manufacturing location string or None
        source       str   "OpenFoodFacts" | "GS1 Verified-by-GS1" | None
        abr          dict  ABR result dict, or None if skipped / no brand found
        confidence   int   rough confidence score 0-100
        pipeline     list  ordered list of pipeline steps executed
        errors       list  non-fatal error messages collected during pipeline
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
            "success":     False,
            "barcode":     barcode,
            "error":       validation["error"],
            "pipeline":    pipeline,
            "errors":      errors,
            "confidence":  0,
        }

    clean_barcode = validation["digits"]

    # ----------------------------------------------------------
    # Step 2 — OpenFoodFacts  (primary)
    # ----------------------------------------------------------
    pipeline.append("OpenFoodFacts product lookup")
    off_result = _lookup_openfoodfacts(clean_barcode)
    product_data: Optional[Dict[str, Any]] = None

    if off_result["success"]:
        product_data = off_result
    else:
        # Record the failure as a non-fatal error and try the fallback.
        errors.append(f"OpenFoodFacts: {off_result.get('message', 'unknown error')}")

        # ----------------------------------------------------------
        # Step 3 — GS1 Verified-by-GS1  (fallback)
        # ----------------------------------------------------------
        pipeline.append("GS1 Verified-by-GS1 fallback lookup")
        gs1_result = _lookup_gs1_verified(clean_barcode)

        if gs1_result["success"]:
            product_data = gs1_result
        else:
            errors.append(f"GS1: {gs1_result.get('message', 'unknown error')}")

    # Both APIs failed — return a not-found result.
    if product_data is None:
        return {
            "success":    False,
            "barcode":    clean_barcode,
            "error":      "Product not found in OpenFoodFacts or GS1 registry.",
            "pipeline":   pipeline,
            "errors":     errors,
            "confidence": 0,
        }

    # ----------------------------------------------------------
    # Step 4 — Extract and normalise brand
    # ----------------------------------------------------------
    pipeline.append("Brand extraction and normalisation")
    brand_raw   = product_data.get("brand_raw")   # raw comma-separated brands
    brand_owner = product_data.get("brand_owner") # preferred: legal entity name
    brand_clean = extract_best_brand(brand_raw, brand_owner)

    # ----------------------------------------------------------
    # Step 5 — ABR name lookup  (optional, skip if no brand found)
    # ----------------------------------------------------------
    abr_result: Optional[Dict[str, Any]] = None

    # Use brand_owner first for ABR lookup as it is the legal entity name.
    # Fall back to the normalised brand token if brand_owner is not available.
    abr_search_term = brand_owner or brand_clean

    if abr_lookup_fn and abr_search_term:
        pipeline.append(f"ABR name lookup: '{abr_search_term}'")
        abr_result = abr_lookup_fn(abr_search_term)

        # If brand_owner search fails, retry with the cleaned brand token.
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
    # Start at 0 and add points based on data quality.
    confidence = 0
    if product_data["success"]:
        confidence += 40   # product record found
    if brand_raw:
        confidence += 15   # at least one brand name present
    if brand_owner:
        confidence += 15   # legal entity name available
    if brand_clean:
        confidence += 10   # normalised brand usable for trademark search
    if abr_result and abr_result.get("success"):
        confidence += 20   # ABN verified via ABR

    # ----------------------------------------------------------
    # Unified result
    # ----------------------------------------------------------
    return {
        "success":      True,
        "barcode":      clean_barcode,
        "product": {
            "product_name": product_data.get("product_name"),
            "image_url":    product_data.get("image_url"),
            "categories":   product_data.get("categories"),
            "countries":    product_data.get("countries"),
        },
        "brand_raw":    brand_raw,
        "brand_clean":  brand_clean,
        "brand_owner":  brand_owner,
        "manufacturer": product_data.get("manufacturer"),
        "source":       product_data.get("source"),
        "abr":          abr_result,
        "confidence":   confidence,
        "pipeline":     pipeline,
        "errors":       errors,
    }


# ============================================================
# Section 6 — Convenience wrapper for main.py
# ============================================================

def run_barcode_phase(
    barcode: str,
    abr_lookup_fn=None,
) -> Dict[str, Any]:
    """
    Thin wrapper used by the /api/search barcode branch in main.py.

    Returns the resolve_barcode result unchanged but also includes a
    frontend-friendly `status` field for consistency with other pipeline
    result shapes.

    Usage in main.py
    ----------------
    Replace the barcode block in /api/search with:

        from barcode_pipeline import run_barcode_phase

        # Inside the barcode branch:
        barcode_result = run_barcode_phase(
            barcode,
            abr_lookup_fn=search_company_name_with_abr,
        )
        db_status = "resolved" if barcode_result["success"] else "failed"

        result = {
            "input_type":     "barcode",
            "input_value":    barcode,
            "status":         "external_resolved" if barcode_result["success"] else "not_found",
            "source":         barcode_result.get("source"),
            "product":        barcode_result.get("product"),
            "brand_raw":      barcode_result.get("brand_raw"),
            "brand_clean":    barcode_result.get("brand_clean"),
            "brand_owner":    barcode_result.get("brand_owner"),
            "manufacturer":   barcode_result.get("manufacturer"),
            "abn_verification": barcode_result.get("abr"),
            "confidence":     barcode_result.get("confidence", 0),
            "pipeline_steps": barcode_result.get("pipeline", []),
            "errors":         barcode_result.get("errors", []),
        }
    """
    resolved = resolve_barcode(barcode, abr_lookup_fn=abr_lookup_fn)
    resolved["status"] = "external_resolved" if resolved["success"] else "not_found"
    return resolved
