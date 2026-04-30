"""
brand_pipeline.py
=================

Purpose
-------
Handles the complete brand name input phase of the EcoTrace search pipeline.

Brand pipeline flow
-------------------
  User input (brand name string)
      │
      ├─── 1. Validate     ─── minimum length, strip whitespace
      │
      ├─── 2. OAuth token  ─── IP Australia client_credentials flow (cached)
      │
      ├─── 3. TM Search    ─── IP Australia Trade Mark Search API quick search
      │                          returns up to N trademark records
      │
      ├─── 4. Owner pick   ─── rank by status (Registered > Filed > Lapsed)
      │                          extract applicant / owner name from best match
      │
      └─── 5. ABR verify   ─── ABR name search using extracted legal owner

External APIs
-------------
- IP Australia OAuth token
  POST https://test.api.ipaustralia.gov.au/public/external-token-api/v1/access_token
  Required .env: IP_AUSTRALIA_CLIENT_ID, IP_AUSTRALIA_CLIENT_SECRET
  Optional .env: IP_AUSTRALIA_TOKEN_URL  (override for production)

- IP Australia Trade Mark Search (quick search)
  POST https://test.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1/search/quick
  Optional .env: IP_AUSTRALIA_TRADEMARK_URL  (override for production)

- ABR (passed in as abr_lookup_fn to avoid circular imports)

Usage
-----
From main.py brand branch:

    from brand_pipeline import run_brand_phase

    brand_result = run_brand_phase(
        brand_name,
        abr_lookup_fn=search_company_name_with_abr,
    )

The get_ip_australia_access_token function is also imported into main.py
for the /api/trademark/token-test diagnostic endpoint.
"""

import os
import time
from typing import Any, Dict, List, Optional

import requests


# ============================================================
# Constants
# ============================================================

# Token cache — shared across all requests within one server process.
# This prevents fetching a new token for every trademark search.
_TOKEN_CACHE: Dict[str, Any] = {
    "access_token": None,
    "expires_at":   0,
}

# Trademark status preference order for ranking.
# A 'Registered' mark is far more reliable for owner identification than
# a 'Filed' or 'Lapsed' one.
_TM_STATUS_RANK: Dict[str, int] = {
    "registered":  1,
    "accepted":    2,
    "filed":       3,
    "opposed":     4,
    "lapsed":      5,
    "removed":     6,
    "rejected":    7,
    "withdrawn":   8,
    "abandoned":   9,
}


# ============================================================
# Section 1 — OAuth Token Management
# ============================================================

def get_ip_australia_access_token() -> Optional[str]:
    """
    Obtain a cached OAuth 2.0 access token from IP Australia.

    Uses the client_credentials grant type.
    Token is cached in-process and reused until 60 seconds before expiry.

    Required .env variables:
    - IP_AUSTRALIA_CLIENT_ID
    - IP_AUSTRALIA_CLIENT_SECRET

    Optional .env variables:
    - IP_AUSTRALIA_TOKEN_URL  (defaults to test environment)

    Returns the access token string, or None if credentials are missing
    or the token request fails.
    """
    now = int(time.time())

    # Return cached token if still valid (with 60-second safety buffer).
    if _TOKEN_CACHE["access_token"] and now < int(_TOKEN_CACHE["expires_at"]) - 60:
        return _TOKEN_CACHE["access_token"]

    client_id     = (os.getenv("IP_AUSTRALIA_CLIENT_ID")     or "").strip()
    client_secret = (os.getenv("IP_AUSTRALIA_CLIENT_SECRET") or "").strip()

    if not client_id or not client_secret:
        print("[brand_pipeline] IP Australia credentials missing in .env")
        return None

    token_url = (
        os.getenv(
            "IP_AUSTRALIA_TOKEN_URL",
            "https://test.api.ipaustralia.gov.au/public/external-token-api/v1/access_token",
        ) or ""
    ).strip()

    try:
        response = requests.post(
            token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
            },
            timeout=20,
        )

        if not response.ok:
            print(f"[brand_pipeline] Token request failed: HTTP {response.status_code}")
            print(f"[brand_pipeline] Response body: {response.text[:300]}")
            return None

        token_data    = response.json()
        access_token  = token_data.get("access_token")
        expires_in    = int(token_data.get("expires_in", 3600))

        if not access_token:
            print("[brand_pipeline] Token response missing access_token field")
            return None

        # Store in cache.
        _TOKEN_CACHE["access_token"] = access_token
        _TOKEN_CACHE["expires_at"]   = now + expires_in
        return access_token

    except requests.exceptions.Timeout:
        print("[brand_pipeline] Token request timed out")
        return None
    except Exception as e:
        print(f"[brand_pipeline] Token request exception: {e!r}")
        return None


# ============================================================
# Section 2 — IP Australia Trade Mark Search
# ============================================================

def _search_trademarks(brand_name: str) -> Dict[str, Any]:
    """
    Query the IP Australia Trade Mark Search quick search endpoint.

    Sends a POST request with the brand name as the query string.
    Returns the raw API response dict on success, or an error dict.

    API endpoint:
        POST /search/quick

    Request payload:
        {"query": "<brand_name>"}

    The quick search returns a list of trademark records. Each record
    contains the trademark text, status, filing date, and applicant details.

    Required: valid OAuth token from get_ip_australia_access_token().
    """
    base_url = (
        os.getenv(
            "IP_AUSTRALIA_TRADEMARK_URL",
            "https://test.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1",
        ) or ""
    ).rstrip("/")

    access_token = get_ip_australia_access_token()
    if not access_token:
        return {
            "success": False,
            "message": "Unable to obtain IP Australia OAuth token. "
                       "Check IP_AUSTRALIA_CLIENT_ID and IP_AUSTRALIA_CLIENT_SECRET in .env.",
        }

    url     = f"{base_url}/search/quick"
    headers = {
        "Accept":        "application/json",
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {access_token}",
        "User-Agent":    "EcoTrace-App/1.0 (student project)",
    }
    payload = {"query": brand_name}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)

        if response.status_code == 401:
            # Token may have expired mid-request despite the cache. Clear and retry once.
            _TOKEN_CACHE["access_token"] = None
            _TOKEN_CACHE["expires_at"]   = 0
            access_token = get_ip_australia_access_token()
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"
                response = requests.post(url, headers=headers, json=payload, timeout=20)

        if not response.ok:
            return {
                "success":     False,
                "status_code": response.status_code,
                "message":     response.text[:500],
            }

        return {"success": True, "data": response.json()}

    except requests.exceptions.Timeout:
        return {"success": False, "message": "IP Australia request timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "message": f"IP Australia network error: {e}"}
    except Exception as e:
        return {"success": False, "message": f"IP Australia unexpected error: {e}"}


# ============================================================
# Section 3 — Trademark Owner Extraction
# ============================================================

def _extract_owner_from_record(record: Dict[str, Any]) -> Optional[str]:
    """
    Extract the legal owner / applicant name from a single trademark record.

    IP Australia returns applicant/owner names in several possible fields
    depending on the API version and record type. This function checks
    fields in priority order:

    Priority order:
    1. applicants[0].name       (most common in quick search results)
    2. applicants[0].fullName
    3. owners[0].name
    4. holders[0].name
    5. applicant.name           (singular form)
    6. owner.name               (singular form)
    7. ownerName                (flat field)
    8. applicantName            (flat field)

    Returns the first non-empty string found, or None.
    """
    def first_name_from_list(lst: Any) -> Optional[str]:
        """Get name from first item in a list of owner-like objects."""
        if not isinstance(lst, list) or not lst:
            return None
        item = lst[0]
        if isinstance(item, str):
            return item.strip() or None
        if isinstance(item, dict):
            return (
                item.get("name")
                or item.get("fullName")
                or item.get("organisationName")
                or item.get("legalName")
                or None
            )
        return None

    if not isinstance(record, dict):
        return None

    # Check list-form fields first (most common in IP Australia responses).
    for list_key in ("applicants", "owners", "holders", "proprietors"):
        name = first_name_from_list(record.get(list_key))
        if name:
            return name.strip()

    # Check singular object fields.
    for obj_key in ("applicant", "owner", "holder", "proprietor"):
        obj = record.get(obj_key)
        if isinstance(obj, dict):
            name = obj.get("name") or obj.get("fullName") or obj.get("organisationName")
            if name:
                return name.strip()
        elif isinstance(obj, str) and obj.strip():
            return obj.strip()

    # Check flat string fields.
    for flat_key in ("ownerName", "applicantName", "holderName", "proprietorName"):
        val = record.get(flat_key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    return None


def _rank_trademark_record(record: Dict[str, Any]) -> int:
    """
    Return a sort key for a trademark record.

    Lower number = higher priority (better to use as the source of truth).
    Registered marks outrank Filed marks; both outrank Lapsed/Removed.
    """
    status = str(
        record.get("status")
        or record.get("tradeMarkStatus")
        or record.get("tmStatus")
        or ""
    ).lower().strip()

    return _TM_STATUS_RANK.get(status, 99)


def _parse_trademark_results(raw_data: Any, brand_name: str) -> Dict[str, Any]:
    """
    Parse the IP Australia API response and select the best trademark record.

    Handles two common response shapes:
    Shape A: {"tradeMarks": [...], "totalCount": N}
    Shape B: {"data": [...], "total": N}
    Shape C: a plain list at the top level

    After normalising to a list of records:
    1. Filter to records whose wordMark / tradeMarkText contains the brand.
    2. Sort by status rank (Registered first).
    3. Extract the owner from the best-ranked record.
    4. Return a summary dict with the top match and all candidates.
    """
    records: List[Dict] = []

    if isinstance(raw_data, list):
        records = raw_data
    elif isinstance(raw_data, dict):
        # Try all known list-container keys in order of likelihood.
        for key in ("tradeMarks", "data", "results", "items", "records"):
            if isinstance(raw_data.get(key), list):
                records = raw_data[key]
                break

    if not records:
        return {
            "found":       False,
            "legal_owner": None,
            "trademark":   None,
            "candidates":  [],
            "total":       0,
        }

    # Filter to records that mention the brand name (case-insensitive).
    brand_lower = brand_name.lower()
    relevant = [
        r for r in records
        if isinstance(r, dict) and brand_lower in str(
            r.get("wordMark")
            or r.get("tradeMarkText")
            or r.get("mark")
            or r.get("name")
            or ""
        ).lower()
    ]

    # Fall back to all records if none matched the brand text.
    # (Sometimes the search returns exact matches only.)
    candidates = relevant if relevant else records

    # Sort by status preference (Registered first).
    candidates_sorted = sorted(candidates, key=_rank_trademark_record)

    best = candidates_sorted[0] if candidates_sorted else None
    legal_owner = _extract_owner_from_record(best) if best else None

    # Build a clean summary of the best matching trademark.
    trademark_summary = None
    if best:
        trademark_summary = {
            "number":          best.get("number")         or best.get("applicationNumber") or best.get("tmNumber"),
            "word_mark":       best.get("wordMark")       or best.get("tradeMarkText")     or best.get("mark"),
            "status":          best.get("status")         or best.get("tradeMarkStatus")   or best.get("tmStatus"),
            "filing_date":     best.get("filingDate")     or best.get("applicationDate"),
            "registration_date": best.get("registrationDate"),
            "goods_services":  best.get("goodsAndServices") or best.get("niceClasses"),
            "legal_owner":     legal_owner,
        }

    return {
        "found":       best is not None,
        "legal_owner": legal_owner,
        "trademark":   trademark_summary,
        "candidates":  len(candidates_sorted),
        "total":       len(records),
    }


# ============================================================
# Section 4 — Orchestrated Brand Resolution
# ============================================================

def resolve_brand(
    brand_name:    str,
    abr_lookup_fn: Any = None,
) -> Dict[str, Any]:
    """
    Full brand name resolution pipeline.

    Steps
    -----
    1. Validate brand_name (minimum 2 characters).
    2. Call IP Australia Trade Mark Search quick search.
    3. Parse results and select the best-ranked trademark record.
    4. Extract legal owner from the selected record.
    5. Run ABR name lookup on the extracted owner (if abr_lookup_fn provided).

    Parameters
    ----------
    brand_name     : str
        Brand name entered by the user.
    abr_lookup_fn  : callable, optional
        Function accepting a name string and returning an ABR result dict.
        Pass `search_company_name_with_abr` from main.py.
        If None, the ABR step is skipped.

    Returns
    -------
    dict with:
        success      bool
        brand_name   str   cleaned input
        trademark    dict  best matching trademark summary (or None)
        legal_owner  str   extracted owner name (or None)
        abr          dict  ABR result (or None if skipped)
        confidence   int   0-100
        pipeline     list  ordered steps executed
        errors       list  non-fatal error messages
    """
    pipeline: List[str] = []
    errors:   List[str] = []

    brand_name = (brand_name or "").strip()

    # ----------------------------------------------------------
    # Step 1 — Validate input
    # ----------------------------------------------------------
    pipeline.append("Brand name validation")
    if len(brand_name) < 2:
        return {
            "success":    False,
            "brand_name": brand_name,
            "error":      "Brand name must be at least 2 characters.",
            "pipeline":   pipeline,
            "errors":     errors,
            "confidence": 0,
        }

    # ----------------------------------------------------------
    # Step 2 — IP Australia Trade Mark Search
    # ----------------------------------------------------------
    pipeline.append(f"IP Australia trademark search: '{brand_name}'")
    tm_response = _search_trademarks(brand_name)

    if not tm_response.get("success"):
        error_msg = tm_response.get("message", "IP Australia search failed")
        errors.append(error_msg)
        return {
            "success":    False,
            "brand_name": brand_name,
            "error":      error_msg,
            "trademark":  None,
            "legal_owner": None,
            "abr":        None,
            "pipeline":   pipeline,
            "errors":     errors,
            "confidence": 0,
        }

    # ----------------------------------------------------------
    # Step 3 — Parse and rank trademark results
    # ----------------------------------------------------------
    pipeline.append("Trademark result parsing and owner extraction")
    parsed = _parse_trademark_results(tm_response["data"], brand_name)

    if not parsed["found"]:
        errors.append("No trademark records matched the brand name.")

    legal_owner = parsed.get("legal_owner")

    # ----------------------------------------------------------
    # Step 4 — ABR name lookup  (optional)
    # ----------------------------------------------------------
    abr_result: Optional[Dict[str, Any]] = None

    if abr_lookup_fn and legal_owner:
        pipeline.append(f"ABR name lookup: '{legal_owner}'")
        abr_result = abr_lookup_fn(legal_owner)

        # If the full legal name fails (e.g. 'Arnott's Biscuits Holdings Pty Ltd'),
        # strip legal suffixes and retry with the shorter trading name.
        if not abr_result.get("success"):
            short_name = _strip_legal_suffix(legal_owner)
            if short_name and short_name != legal_owner:
                pipeline.append(f"ABR retry with short name: '{short_name}'")
                abr_result = abr_lookup_fn(short_name)

    # ----------------------------------------------------------
    # Confidence scoring
    # ----------------------------------------------------------
    confidence = 0
    if parsed["found"]:
        confidence += 30  # at least one trademark found
    tm = parsed.get("trademark") or {}
    if str(tm.get("status") or "").lower() == "registered":
        confidence += 25  # registered trademark is strong signal
    elif tm.get("status"):
        confidence += 10  # other status still gives some signal
    if legal_owner:
        confidence += 20  # owner name successfully extracted
    if abr_result and abr_result.get("success"):
        confidence += 25  # ABN verified via ABR

    # ----------------------------------------------------------
    # Unified result
    # ----------------------------------------------------------
    return {
        "success":     parsed["found"] or abr_result is not None,
        "brand_name":  brand_name,
        "trademark":   parsed.get("trademark"),
        "legal_owner": legal_owner,
        "abr":         abr_result,
        "tm_candidates": parsed.get("candidates", 0),
        "tm_total":    parsed.get("total", 0),
        "confidence":  confidence,
        "pipeline":    pipeline,
        "errors":      errors,
    }


def _strip_legal_suffix(name: str) -> str:
    """
    Remove common ASIC legal entity suffixes from a company name.

    Used to shorten 'Arnott's Biscuits Holdings Pty Ltd' to
    'Arnott's Biscuits Holdings' for a broader ABR search.
    """
    import re
    patterns = [
        r"\bPty\.?\s*Ltd\.?",
        r"\bPty\.?",
        r"\bLtd\.?",
        r"\bLimited\b",
        r"\bInc\.?",
        r"\bCorp\.?",
        r"\bLLC\.?",
        r"\bCo\.?",
        r"\bHoldings\b",
    ]
    result = name
    for p in patterns:
        result = re.sub(p, "", result, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", result).strip()


# ============================================================
# Section 5 — Convenience wrapper for main.py
# ============================================================

def run_brand_phase(
    brand_name:    str,
    abr_lookup_fn: Any = None,
) -> Dict[str, Any]:
    """
    Thin wrapper used by the /api/search brand branch in main.py.

    Calls resolve_brand and adds a frontend-friendly `status` field.

    Usage in main.py
    ----------------
        from brand_pipeline import run_brand_phase

        brand_result = run_brand_phase(
            brand,
            abr_lookup_fn=search_company_name_with_abr,
        )
        db_status = "resolved" if brand_result["success"] else "failed"
    """
    resolved = resolve_brand(brand_name, abr_lookup_fn=abr_lookup_fn)
    resolved["status"] = "external_resolved" if resolved["success"] else "not_found"
    return resolved
