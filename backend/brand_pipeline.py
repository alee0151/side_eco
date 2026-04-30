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
      ├─── 3. TM Search    ─── POST /search/quick → returns {trademarkIds, count}
      │                           NOTE: quick search returns IDs only, not full records
      │
      ├─── 4. TM Detail    ─── GET /trademarks/{id} for the first (best) ID
      │                           returns full record: owner, status, wordMark, etc.
      │
      ├─── 5. Owner pick   ─── extract applicant / owner name from detail record
      │
      └─── 6. ABR verify   ─── ABR name search using extracted legal owner

External APIs
-------------
- IP Australia OAuth token
  POST https://test.api.ipaustralia.gov.au/public/external-token-api/v1/access_token

- IP Australia Trade Mark Quick Search (returns IDs only)
  POST https://test.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1/search/quick
  Response: {"trademarkIds": ["123", ...], "count": N, "aggregations": {...}}

- IP Australia Trade Mark Detail (returns full record)
  GET  https://test.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1/trademarks/{id}

Usage
-----
    from brand_pipeline import run_brand_phase
    brand_result = run_brand_phase(brand_name, abr_lookup_fn=search_company_name_with_abr)
"""

import os
import time
from typing import Any, Dict, List, Optional

import requests


# ============================================================
# Constants
# ============================================================

_TOKEN_CACHE: Dict[str, Any] = {
    "access_token": None,
    "expires_at":   0,
}

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
    Token is cached in-process and reused until 60 seconds before expiry.
    """
    now = int(time.time())
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
            return None

        token_data   = response.json()
        access_token = token_data.get("access_token")
        expires_in   = int(token_data.get("expires_in", 3600))

        if not access_token:
            return None

        _TOKEN_CACHE["access_token"] = access_token
        _TOKEN_CACHE["expires_at"]   = now + expires_in
        return access_token

    except requests.exceptions.Timeout:
        print("[brand_pipeline] Token request timed out")
        return None
    except Exception as e:
        print(f"[brand_pipeline] Token request exception: {e!r}")
        return None


def _get_auth_headers() -> Optional[Dict[str, str]]:
    """Build authorisation headers, refreshing the token if needed."""
    token = get_ip_australia_access_token()
    if not token:
        return None
    return {
        "Accept":        "application/json",
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent":    "EcoTrace-App/1.0 (student project)",
    }


def _base_url() -> str:
    return (
        os.getenv(
            "IP_AUSTRALIA_TRADEMARK_URL",
            "https://test.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1",
        ) or ""
    ).rstrip("/")


# ============================================================
# Section 2 — Quick Search (returns IDs only)
# ============================================================

def _quick_search(brand_name: str) -> Dict[str, Any]:
    """
    POST /search/quick → {"trademarkIds": [...], "count": N, "aggregations": {...}}

    Returns the raw response dict.  The caller must then fetch a detail
    record for whichever ID it wants to use.
    """
    headers = _get_auth_headers()
    if not headers:
        return {
            "success": False,
            "message": "Unable to obtain IP Australia OAuth token. "
                       "Check IP_AUSTRALIA_CLIENT_ID and IP_AUSTRALIA_CLIENT_SECRET in .env.",
        }

    url = f"{_base_url()}/search/quick"
    try:
        resp = requests.post(url, headers=headers, json={"query": brand_name}, timeout=20)

        if resp.status_code == 401:
            # Token expired mid-flight — clear cache and retry once.
            _TOKEN_CACHE["access_token"] = None
            _TOKEN_CACHE["expires_at"]   = 0
            headers = _get_auth_headers()
            if headers:
                resp = requests.post(url, headers=headers, json={"query": brand_name}, timeout=20)

        if not resp.ok:
            return {"success": False, "status_code": resp.status_code, "message": resp.text[:500]}

        return {"success": True, "data": resp.json()}

    except requests.exceptions.Timeout:
        return {"success": False, "message": "IP Australia request timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "message": f"IP Australia network error: {e}"}


# ============================================================
# Section 3 — Trademark Detail Lookup
# ============================================================

def _fetch_trademark_detail(trademark_id: str) -> Dict[str, Any]:
    """
    GET /trademarks/{id} → full trademark record.

    Returns the full record dict (wordMark, status, applicants, etc.)
    or an error dict on failure.
    """
    headers = _get_auth_headers()
    if not headers:
        return {"success": False, "message": "No auth token for trademark detail lookup"}

    url = f"{_base_url()}/trademarks/{trademark_id}"
    try:
        resp = requests.get(url, headers=headers, timeout=20)

        if resp.status_code == 401:
            _TOKEN_CACHE["access_token"] = None
            _TOKEN_CACHE["expires_at"]   = 0
            headers = _get_auth_headers()
            if headers:
                resp = requests.get(url, headers=headers, timeout=20)

        if resp.status_code == 404:
            return {"success": False, "message": f"Trademark {trademark_id} not found"}

        if not resp.ok:
            return {"success": False, "status_code": resp.status_code, "message": resp.text[:300]}

        return {"success": True, "data": resp.json()}

    except requests.exceptions.Timeout:
        return {"success": False, "message": "Trademark detail request timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "message": f"Trademark detail network error: {e}"}


def _pick_best_trademark_id(search_data: Dict[str, Any]) -> Optional[str]:
    """
    Choose the best trademark ID from a quick-search response.

    Strategy:
    - The quick search response includes aggregations.status counts.
    - The trademarkIds list is ordered by relevance (highest score first).
    - We simply return the first ID; the list is already relevance-ranked
      by IP Australia and the detail fetch will confirm the status.
    """
    ids = search_data.get("trademarkIds") or []
    if not ids:
        return None
    return str(ids[0])


# ============================================================
# Section 4 — Owner Extraction from Detail Record
# ============================================================

def _extract_owner_from_record(record: Dict[str, Any]) -> Optional[str]:
    """
    Extract the legal owner / applicant name from a trademark detail record.

    IP Australia nests owner names in several possible fields.
    Checks in priority order:
      applicants[0] > owners[0] > holders[0] > singular forms > flat strings.
    """
    def first_name_from_list(lst: Any) -> Optional[str]:
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

    for list_key in ("applicants", "owners", "holders", "proprietors"):
        name = first_name_from_list(record.get(list_key))
        if name:
            return name.strip()

    for obj_key in ("applicant", "owner", "holder", "proprietor"):
        obj = record.get(obj_key)
        if isinstance(obj, dict):
            name = obj.get("name") or obj.get("fullName") or obj.get("organisationName")
            if name:
                return name.strip()
        elif isinstance(obj, str) and obj.strip():
            return obj.strip()

    for flat_key in ("ownerName", "applicantName", "holderName", "proprietorName"):
        val = record.get(flat_key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    return None


def _build_trademark_summary(record: Dict[str, Any], legal_owner: Optional[str]) -> Dict:
    """Flatten a trademark detail record into the shape the rest of the pipeline expects."""
    return {
        "number":             record.get("number")           or record.get("applicationNumber") or record.get("tmNumber"),
        "word_mark":          record.get("wordMark")         or record.get("tradeMarkText")     or record.get("mark"),
        "status":             record.get("status")           or record.get("tradeMarkStatus")   or record.get("tmStatus"),
        "filing_date":        record.get("filingDate")       or record.get("applicationDate"),
        "registration_date":  record.get("registrationDate"),
        "goods_services":     record.get("goodsAndServices") or record.get("niceClasses"),
        "legal_owner":        legal_owner,
    }


# ============================================================
# Section 5 — Orchestrated Brand Resolution
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
    2. POST /search/quick → get trademarkIds list.
    3. Pick first (most relevant) trademark ID.
    4. GET /trademarks/{id} → full trademark detail record.
    5. Extract legal owner from detail record.
    6. ABR name lookup on extracted owner (if abr_lookup_fn provided).
    """
    pipeline: List[str] = []
    errors:   List[str] = []

    brand_name = (brand_name or "").strip()

    # Step 1 — Validate
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

    # Step 2 — Quick search (IDs only)
    pipeline.append(f"IP Australia quick search: '{brand_name}'")
    search_resp = _quick_search(brand_name)

    if not search_resp.get("success"):
        msg = search_resp.get("message", "IP Australia search failed")
        return {
            "success":     False,
            "brand_name":  brand_name,
            "error":       msg,
            "trademark":   None,
            "legal_owner": None,
            "abr":         None,
            "pipeline":    pipeline,
            "errors":      [msg],
            "confidence":  0,
        }

    search_data = search_resp["data"]
    total_ids   = search_data.get("count", 0)

    # Step 3 — Pick best ID
    pipeline.append(f"Select best trademark ID from {total_ids} results")
    best_id = _pick_best_trademark_id(search_data)

    if not best_id:
        return {
            "success":     False,
            "brand_name":  brand_name,
            "error":       f"No trademarks found for '{brand_name}'",
            "trademark":   None,
            "legal_owner": None,
            "abr":         None,
            "pipeline":    pipeline,
            "errors":      errors,
            "confidence":  0,
        }

    # Step 4 — Fetch full detail record
    pipeline.append(f"Fetch trademark detail: ID {best_id}")
    detail_resp = _fetch_trademark_detail(best_id)

    if not detail_resp.get("success"):
        msg = detail_resp.get("message", f"Could not fetch trademark {best_id}")
        errors.append(msg)
        # Non-fatal: we still know a trademark exists; report partial result.
        return {
            "success":     False,
            "brand_name":  brand_name,
            "error":       msg,
            "trademark":   {"number": best_id, "word_mark": None, "status": None, "legal_owner": None},
            "legal_owner": None,
            "abr":         None,
            "tm_total":    total_ids,
            "pipeline":    pipeline,
            "errors":      errors,
            "confidence":  10,   # at least we know the brand has TM records
        }

    record      = detail_resp["data"]
    legal_owner = _extract_owner_from_record(record)
    trademark   = _build_trademark_summary(record, legal_owner)

    # Step 5 — ABR lookup (optional)
    abr_result: Optional[Dict[str, Any]] = None

    if abr_lookup_fn and legal_owner:
        pipeline.append(f"ABR name lookup: '{legal_owner}'")
        abr_result = abr_lookup_fn(legal_owner)

        if not abr_result.get("success"):
            short_name = _strip_legal_suffix(legal_owner)
            if short_name and short_name != legal_owner:
                pipeline.append(f"ABR retry with short name: '{short_name}'")
                abr_result = abr_lookup_fn(short_name)

    # Confidence scoring
    confidence = 0
    if best_id:                                                          confidence += 20
    if str(trademark.get("status") or "").lower() == "registered":       confidence += 30
    elif trademark.get("status"):                                        confidence += 10
    if legal_owner:                                                      confidence += 20
    if abr_result and abr_result.get("success"):                         confidence += 30

    return {
        "success":     True,
        "brand_name":  brand_name,
        "trademark":   trademark,
        "legal_owner": legal_owner,
        "abr":         abr_result,
        "tm_total":    total_ids,
        "confidence":  min(confidence, 100),
        "pipeline":    pipeline,
        "errors":      errors,
    }


def _strip_legal_suffix(name: str) -> str:
    """Remove ASIC suffixes from a company name for a broader ABR search."""
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
# Section 6 — Convenience wrapper for main.py
# ============================================================

def run_brand_phase(
    brand_name:    str,
    abr_lookup_fn: Any = None,
) -> Dict[str, Any]:
    """
    Thin wrapper used by /api/search brand branch in main.py.
    Calls resolve_brand and adds a frontend-friendly `status` field.
    """
    resolved = resolve_brand(brand_name, abr_lookup_fn=abr_lookup_fn)
    resolved["status"] = "external_resolved" if resolved["success"] else "not_found"
    return resolved
