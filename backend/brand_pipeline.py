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
      ├─── 3. TM Search    ─── POST /search/quick
      │                         filters: quickSearchType=WORD, status=REGISTERED
      │                         returns {trademarkIds, count}
      │
      ├─── 4. TM Detail    ─── GET /trade-mark/{ipRightIdentifier}
      │                         walks IDs until one resolves
      │
      ├─── 5. Owner pick   ─── extract from record.owner[] (ApiPartyType[])
      │
      └─── 6. ABR verify   ─── ABR name search using extracted legal owner

External APIs
-------------
- IP Australia OAuth token (PRODUCTION)
  POST https://api.ipaustralia.gov.au/public/external-token-api/v1/access_token

- IP Australia Trade Mark Quick Search (PRODUCTION)
  POST https://production.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1/search/quick
  Body: {"query": str, "filters": {"quickSearchType": ["WORD"], "status": ["REGISTERED"]}}
  Response: {"trademarkIds": [...], "count": N, "aggregations": {...}}

- IP Australia Trade Mark Detail (PRODUCTION)
  GET  https://production.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1/trade-mark/{ipRightIdentifier}
  Response: ApiTrademark — key fields:
    number       : str
    words        : str[]          ← the word-mark text(s)
    statusCode   : str            ← e.g. "Registered"
    statusGroup  : str            ← e.g. "REGISTERED"
    owner        : ApiPartyType[] ← [{name, abn, acnOrArbn, jurisdiction, structuredAddress}]
    goodsAndServices : ApiGoodsServices[]

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

# Maximum number of IDs to try before giving up on the detail lookup.
_TM_DETAIL_MAX_TRIES = 10

# Production base URLs — overridable via .env
_DEFAULT_TOKEN_URL = "https://api.ipaustralia.gov.au/public/external-token-api/v1/access_token"
_DEFAULT_TM_URL    = "https://production.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1"


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

    token_url = (os.getenv("IP_AUSTRALIA_TOKEN_URL") or _DEFAULT_TOKEN_URL).strip()

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
    return (os.getenv("IP_AUSTRALIA_TRADEMARK_URL") or _DEFAULT_TM_URL).rstrip("/")


# ============================================================
# Section 2 — Quick Search (returns IDs only)
# ============================================================

def _quick_search(brand_name: str) -> Dict[str, Any]:
    """
    POST /search/quick with WORD + REGISTERED filters.
    Returns {"trademarkIds": [...], "count": N}
    """
    headers = _get_auth_headers()
    if not headers:
        return {
            "success": False,
            "message": "Unable to obtain IP Australia OAuth token. "
                       "Check IP_AUSTRALIA_CLIENT_ID and IP_AUSTRALIA_CLIENT_SECRET in .env.",
        }

    url  = f"{_base_url()}/search/quick"
    body = {
        "query": brand_name,
        "filters": {
            "quickSearchType": ["WORD"],
            "status":          ["REGISTERED"],
        },
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=20)

        if resp.status_code == 401:
            _TOKEN_CACHE["access_token"] = None
            _TOKEN_CACHE["expires_at"]   = 0
            headers = _get_auth_headers()
            if headers:
                resp = requests.post(url, headers=headers, json=body, timeout=20)

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
    GET /trade-mark/{ipRightIdentifier} → full ApiTrademark record.

    Correct path per the official Swagger spec (api.json):
        /trade-mark/{ipRightIdentifier}   ← hyphenated, singular
    NOT:
        /trademarks/{id}                  ← this was the previous (wrong) path

    404 is surfaced as not_found=True so the caller can try the next ID.
    """
    headers = _get_auth_headers()
    if not headers:
        return {"success": False, "message": "No auth token for trademark detail lookup"}

    # FIX: path is /trade-mark/{id}, not /trademarks/{id}
    url = f"{_base_url()}/trade-mark/{trademark_id}"
    try:
        resp = requests.get(url, headers=headers, timeout=20)

        if resp.status_code == 401:
            _TOKEN_CACHE["access_token"] = None
            _TOKEN_CACHE["expires_at"]   = 0
            headers = _get_auth_headers()
            if headers:
                resp = requests.get(url, headers=headers, timeout=20)

        if resp.status_code == 404:
            return {"success": False, "not_found": True,
                    "message": f"Trademark {trademark_id} not found (404)"}

        if not resp.ok:
            return {"success": False, "status_code": resp.status_code,
                    "message": resp.text[:300]}

        return {"success": True, "data": resp.json()}

    except requests.exceptions.Timeout:
        return {"success": False, "message": "Trademark detail request timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "message": f"Trademark detail network error: {e}"}


def _fetch_first_available_trademark(
    ids: List[str],
    max_tries: int = _TM_DETAIL_MAX_TRIES,
) -> Dict[str, Any]:
    """
    Walk *ids* in relevance order and return the first detail record that
    resolves successfully (HTTP 200). Skips 404s; stops on any other error.

    Returns:
        {"success": True,  "data": <record>, "tried": <n>, "id": <str>}
        {"success": False, "message": <str>,  "tried": <n>}
    """
    skipped: List[str] = []

    for tm_id in [str(i) for i in ids[:max_tries]]:
        resp = _fetch_trademark_detail(tm_id)

        if resp.get("success"):
            if skipped:
                print(f"[brand_pipeline] skipped 404 IDs {skipped}; resolved on {tm_id}")
            return {
                "success": True,
                "data":    resp["data"],
                "tried":   len(skipped) + 1,
                "id":      tm_id,
            }

        if resp.get("not_found"):
            skipped.append(tm_id)
            continue

        # Non-404 error — stop immediately
        return {
            "success": False,
            "message": resp.get("message", "Trademark detail lookup failed"),
            "tried":   len(skipped) + 1,
        }

    return {
        "success": False,
        "message": (
            f"None of the first {max_tries} trademark IDs returned a valid record. "
            f"IDs tried: {skipped}"
        ),
        "tried": len(skipped),
    }


# ============================================================
# Section 4 — Owner Extraction from Detail Record
# ============================================================

def _extract_owner_from_record(record: Dict[str, Any]) -> Optional[str]:
    """
    Extract the legal owner name from an ApiTrademark detail record.

    Per the official Swagger spec (api.json), the correct field is:
        record["owner"]  →  ApiPartyType[]
                            each item: {name, abn, acnOrArbn, jurisdiction, structuredAddress}

    The previous implementation checked non-existent keys like "applicants",
    "holders", "proprietors" which are not in the API response schema.
    """
    if not isinstance(record, dict):
        return None

    # PRIMARY: owner[] — the canonical field per ApiTrademark schema
    owner_list = record.get("owner")
    if isinstance(owner_list, list) and owner_list:
        first = owner_list[0]
        if isinstance(first, dict):
            name = first.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        elif isinstance(first, str) and first.strip():
            return first.strip()

    # FALLBACK: addressForService[] carries the same ApiPartyType shape
    afs_list = record.get("addressForService")
    if isinstance(afs_list, list) and afs_list:
        first = afs_list[0]
        if isinstance(first, dict):
            name = first.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()

    return None


def _build_trademark_summary(record: Dict[str, Any], legal_owner: Optional[str]) -> Dict:
    """
    Flatten an ApiTrademark detail record into the shape the rest of the
    pipeline expects.

    Correct field names per api.json ApiTrademark schema:
      number       → record["number"]
      words        → record["words"]        (str[] — the word-mark text)
      statusCode   → record["statusCode"]   (e.g. "Registered")
      statusGroup  → record["statusGroup"]  (e.g. "REGISTERED")
      goodsAndServices → record["goodsAndServices"]
    """
    # words[] is the actual word-mark; join multiple elements if present
    words = record.get("words") or []
    word_mark = " ".join(words).strip() if isinstance(words, list) else None

    # statusCode is human-readable ("Registered"); statusGroup is the enum ("REGISTERED")
    status = record.get("statusCode") or record.get("statusGroup")

    return {
        "number":            record.get("number"),
        "word_mark":         word_mark or None,
        "status":            status,
        "filing_date":       record.get("filingDate")       or record.get("lodgementDate"),
        "registration_date": record.get("enteredOnRegisterDate"),
        "goods_services":    record.get("goodsAndServices"),
        "legal_owner":       legal_owner,
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
    1. Validate brand_name (minimum 2 characters).
    2. POST /search/quick (WORD + REGISTERED) → get trademarkIds list.
    3. Walk IDs until one detail record resolves (skipping 404s).
    4. Extract legal owner from record.owner[] (ApiPartyType[]).
    5. ABR name lookup on extracted owner (if abr_lookup_fn provided).
    """
    pipeline: List[str] = []
    errors:   List[str] = []

    brand_name = (brand_name or "").strip()

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

    pipeline.append(f"IP Australia quick search (WORD+REGISTERED): '{brand_name}'")
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
    ids         = search_data.get("trademarkIds") or []

    if not ids:
        return {
            "success":     False,
            "brand_name":  brand_name,
            "error":       f"No registered word-mark trademarks found for '{brand_name}'",
            "trademark":   None,
            "legal_owner": None,
            "abr":         None,
            "pipeline":    pipeline,
            "errors":      errors,
            "confidence":  0,
        }

    pipeline.append(f"Fetch trademark detail (up to {_TM_DETAIL_MAX_TRIES} tries) from {total_ids} results")
    detail_resp = _fetch_first_available_trademark(ids)

    if not detail_resp.get("success"):
        msg = detail_resp.get("message", "Could not fetch any trademark detail record")
        errors.append(msg)
        return {
            "success":     False,
            "brand_name":  brand_name,
            "error":       msg,
            "trademark":   None,
            "legal_owner": None,
            "abr":         None,
            "tm_total":    total_ids,
            "pipeline":    pipeline,
            "errors":      errors,
            "confidence":  10,
        }

    resolved_id = detail_resp["id"]
    pipeline.append(f"Resolved trademark ID: {resolved_id} (after {detail_resp['tried']} attempt(s))")

    record      = detail_resp["data"]
    legal_owner = _extract_owner_from_record(record)
    trademark   = _build_trademark_summary(record, legal_owner)

    abr_result: Optional[Dict[str, Any]] = None

    if abr_lookup_fn and legal_owner:
        pipeline.append(f"ABR name lookup: '{legal_owner}'")
        abr_result = abr_lookup_fn(legal_owner)

        if not abr_result.get("success"):
            short_name = _strip_legal_suffix(legal_owner)
            if short_name and short_name != legal_owner:
                pipeline.append(f"ABR retry with short name: '{short_name}'")
                abr_result = abr_lookup_fn(short_name)

    confidence = 0
    if resolved_id:                                                               confidence += 20
    if str(trademark.get("status") or "").lower() == "registered":               confidence += 30
    elif trademark.get("status"):                                                 confidence += 10
    if legal_owner:                                                               confidence += 20
    if abr_result and abr_result.get("success"):                                  confidence += 30

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
    """Thin wrapper used by /api/search brand branch in main.py."""
    resolved = resolve_brand(brand_name, abr_lookup_fn=abr_lookup_fn)
    resolved["status"] = "external_resolved" if resolved["success"] else "not_found"
    return resolved
