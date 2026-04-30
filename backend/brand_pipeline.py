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
      │                         Token URL : IP_AUSTRALIA_TOKEN_URL  (default: test instance)
      │                         Method    : Basic Auth header first, body params fallback
      │
      ├─── 3. TM Search    ─── POST /search/quick
      │                         TM URL   : IP_AUSTRALIA_TRADEMARK_URL (default: production)
      │                         filters  : quickSearchType=WORD, status=REGISTERED
      │                         returns  : {trademarkIds, count}
      │
      ├─── 4. TM Detail    ─── GET /trade-mark/{ipRightIdentifier}
      │                         walks IDs until one resolves
      │
      ├─── 5. Owner pick   ─── extract from record.owner[] (ApiPartyType[])
      │
      └─── 6. ABR verify   ─── ABR name search using extracted legal owner

OAuth notes
-----------
IP Australia uses a MuleSoft gateway.  MuleSoft OAuth servers expect the
client credentials in an HTTP Basic Authentication header:

    Authorization: Basic base64(client_id:client_secret)
    Content-Type: application/x-www-form-urlencoded
    Body: grant_type=client_credentials

This implementation tries Basic Auth first and falls back to body params
(RFC 6749 §2.3.1) if the server returns 401 or 400.

Environment variables  (set in backend/.env)
--------------------------------------------
  IP_AUSTRALIA_CLIENT_ID       your OAuth client id
  IP_AUSTRALIA_CLIENT_SECRET   your OAuth client secret
  IP_AUSTRALIA_TOKEN_URL       token endpoint  (default: test instance)
  IP_AUSTRALIA_TRADEMARK_URL   TM Search API   (default: production instance)

Test with the diagnostic endpoint before running a full search:
    GET /api/debug/trademark-auth

External APIs
-------------
- IP Australia OAuth token  (IP_AUSTRALIA_TOKEN_URL)
  POST https://test.api.ipaustralia.gov.au/public/external-token-api/v1/access_token

- IP Australia Trade Mark Quick Search  (IP_AUSTRALIA_TRADEMARK_URL)
  POST .../search/quick
  Body: {"query": str, "filters": {"quickSearchType": ["WORD"], "status": ["REGISTERED"]}}
  Response: {"trademarkIds": [...], "count": N}

- IP Australia Trade Mark Detail  (IP_AUSTRALIA_TRADEMARK_URL)
  GET .../trade-mark/{ipRightIdentifier}
  Response: ApiTrademark (owner[], words[], statusCode, statusGroup, ...)

Usage
-----
    from brand_pipeline import run_brand_phase
    brand_result = run_brand_phase(brand_name, abr_lookup_fn=search_company_name_with_abr)
"""

import base64
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

# ---------------------------------------------------------------
# Confirmed default URLs  (overridable via .env)
#
# TOKEN_URL    : TEST       instance  (confirmed)
# TRADEMARK_URL: PRODUCTION instance  (confirmed)
# ---------------------------------------------------------------
_DEFAULT_TOKEN_URL = "https://test.api.ipaustralia.gov.au/public/external-token-api/v1/access_token"
_DEFAULT_TM_URL    = "https://production.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1"


# ============================================================
# Section 1 — URL helpers
# ============================================================

def _token_url() -> str:
    """OAuth token endpoint — reads IP_AUSTRALIA_TOKEN_URL from env."""
    return (os.getenv("IP_AUSTRALIA_TOKEN_URL") or _DEFAULT_TOKEN_URL).strip()


def _base_url() -> str:
    """Trade Mark Search API base — reads IP_AUSTRALIA_TRADEMARK_URL from env."""
    return (os.getenv("IP_AUSTRALIA_TRADEMARK_URL") or _DEFAULT_TM_URL).rstrip("/")


# ============================================================
# Section 2 — OAuth Token Management
# ============================================================

def _make_token_request(client_id: str, client_secret: str) -> Optional[requests.Response]:
    """
    Attempt to obtain an OAuth token using two methods:

    Method A (preferred) — HTTP Basic Auth header:
        Authorization: Basic base64(client_id:client_secret)
        Body: grant_type=client_credentials

    Method B (fallback) — credentials in request body (RFC 6749 §2.3.1):
        Body: grant_type=client_credentials&client_id=...&client_secret=...

    MuleSoft-based gateways (IP Australia) typically require Method A.
    Returns the first successful Response, or None if both fail.
    """
    url = _token_url()
    basic_creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    # --- Method A: Basic Auth header ---
    try:
        resp = requests.post(
            url,
            headers={
                "Content-Type":  "application/x-www-form-urlencoded",
                "Accept":        "application/json",
                "Authorization": f"Basic {basic_creds}",
            },
            data={"grant_type": "client_credentials"},
            timeout=20,
        )
        print(f"[brand_pipeline] Token (Basic Auth): HTTP {resp.status_code}")
        if resp.status_code not in (400, 401, 403):
            return resp
        print(f"[brand_pipeline] Basic Auth rejected ({resp.status_code}), trying body params...")
    except requests.exceptions.Timeout:
        print("[brand_pipeline] Token request (Basic Auth) timed out")
    except requests.exceptions.RequestException as e:
        print(f"[brand_pipeline] Token request (Basic Auth) error: {e}")

    # --- Method B: credentials in body ---
    try:
        resp = requests.post(
            url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept":       "application/json",
            },
            data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
            },
            timeout=20,
        )
        print(f"[brand_pipeline] Token (body params): HTTP {resp.status_code}")
        return resp
    except requests.exceptions.Timeout:
        print("[brand_pipeline] Token request (body params) timed out")
    except requests.exceptions.RequestException as e:
        print(f"[brand_pipeline] Token request (body params) error: {e}")

    return None


def get_ip_australia_access_token() -> Optional[str]:
    """
    Obtain (or return cached) IP Australia OAuth token.
    Cached in-process and reused until 60 s before expiry.
    """
    now = int(time.time())
    if _TOKEN_CACHE["access_token"] and now < int(_TOKEN_CACHE["expires_at"]) - 60:
        return _TOKEN_CACHE["access_token"]

    client_id     = (os.getenv("IP_AUSTRALIA_CLIENT_ID")     or "").strip()
    client_secret = (os.getenv("IP_AUSTRALIA_CLIENT_SECRET") or "").strip()

    # ---- Guard: credentials not configured at all ----
    if not client_id or not client_secret:
        print(
            "[brand_pipeline] ERROR: IP_AUSTRALIA_CLIENT_ID or IP_AUSTRALIA_CLIENT_SECRET "
            "is missing from .env.  Copy backend/.env.example to backend/.env and fill in "
            "your credentials."
        )
        return None

    print(f"[brand_pipeline] Fetching OAuth token from: {_token_url()}")
    resp = _make_token_request(client_id, client_secret)

    if resp is None:
        print("[brand_pipeline] All token request attempts failed (network / timeout).")
        return None

    if not resp.ok:
        print(
            f"[brand_pipeline] Token endpoint returned HTTP {resp.status_code}.\n"
            f"  URL   : {_token_url()}\n"
            f"  Body  : {resp.text[:300]}\n"
            "  Check that IP_AUSTRALIA_CLIENT_ID and IP_AUSTRALIA_CLIENT_SECRET "
            "are correct and that your app subscription is active on the portal."
        )
        return None

    token_data   = resp.json()
    access_token = token_data.get("access_token")
    expires_in   = int(token_data.get("expires_in", 3600))

    if not access_token:
        print(f"[brand_pipeline] Token response had no access_token field: {token_data}")
        return None

    _TOKEN_CACHE["access_token"] = access_token
    _TOKEN_CACHE["expires_at"]   = now + expires_in
    print(f"[brand_pipeline] Token obtained, expires in {expires_in}s.")
    return access_token


def _get_auth_headers() -> Optional[Dict[str, str]]:
    """Build Bearer authorisation headers, refreshing the token if needed."""
    token = get_ip_australia_access_token()
    if not token:
        return None
    return {
        "Accept":        "application/json",
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent":    "EcoTrace-App/1.0 (student project)",
    }


# ============================================================
# Section 3 — Quick Search (returns IDs only)
# ============================================================

def _quick_search(brand_name: str) -> Dict[str, Any]:
    """
    POST {IP_AUSTRALIA_TRADEMARK_URL}/search/quick
    Filters: WORD + REGISTERED
    Returns {"trademarkIds": [...], "count": N}
    """
    headers = _get_auth_headers()
    if not headers:
        return {
            "success": False,
            "message": (
                "Unable to obtain IP Australia OAuth token. "
                "Check that backend/.env exists with valid "
                "IP_AUSTRALIA_CLIENT_ID and IP_AUSTRALIA_CLIENT_SECRET. "
                f"Token URL in use: {_token_url()}"
            ),
        }

    url  = f"{_base_url()}/search/quick"
    body = {
        "query": brand_name,
        "filters": {
            "quickSearchType": ["WORD"],
            "status":          ["REGISTERED"],
        },
    }

    print(f"[brand_pipeline] Quick search POST: {url}  query={brand_name!r}")

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=20)

        if resp.status_code == 401:
            # Token may have expired mid-request — clear and retry once
            _TOKEN_CACHE["access_token"] = None
            _TOKEN_CACHE["expires_at"]   = 0
            headers = _get_auth_headers()
            if headers:
                resp = requests.post(url, headers=headers, json=body, timeout=20)

        if not resp.ok:
            return {"success": False, "status_code": resp.status_code, "message": resp.text[:500]}

        return {"success": True, "data": resp.json()}

    except requests.exceptions.Timeout:
        return {"success": False, "message": "IP Australia quick search timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "message": f"IP Australia network error: {e}"}


# ============================================================
# Section 4 — Trademark Detail Lookup
# ============================================================

def _fetch_trademark_detail(trademark_id: str) -> Dict[str, Any]:
    """
    GET {IP_AUSTRALIA_TRADEMARK_URL}/trade-mark/{ipRightIdentifier}
    Returns full ApiTrademark record.
    404 surfaced as not_found=True so caller can try the next ID.
    """
    headers = _get_auth_headers()
    if not headers:
        return {"success": False, "message": "No auth token for trademark detail lookup"}

    url = f"{_base_url()}/trade-mark/{trademark_id}"
    print(f"[brand_pipeline] Detail GET: {url}")

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
# Section 5 — Owner Extraction from Detail Record
# ============================================================

def _extract_owner_from_record(record: Dict[str, Any]) -> Optional[str]:
    """
    Extract the legal owner name from an ApiTrademark detail record.

    Per the official Swagger spec (api.json):
        record["owner"]  →  ApiPartyType[]
                            each item: {name, abn, acnOrArbn, jurisdiction, structuredAddress}
    """
    if not isinstance(record, dict):
        return None

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
    Flatten an ApiTrademark record into the shape the pipeline expects.

    Field mapping (per api.json ApiTrademark schema):
      words[]               → word_mark  (str[] joined)
      statusCode            → status     (e.g. "Registered")
      statusGroup           → status     (fallback, e.g. "REGISTERED")
      filingDate /
        lodgementDate       → filing_date
      enteredOnRegisterDate → registration_date
      goodsAndServices      → goods_services
    """
    words = record.get("words") or []
    word_mark = " ".join(words).strip() if isinstance(words, list) else None
    status = record.get("statusCode") or record.get("statusGroup")

    return {
        "number":            record.get("number"),
        "word_mark":         word_mark or None,
        "status":            status,
        "filing_date":       record.get("filingDate") or record.get("lodgementDate"),
        "registration_date": record.get("enteredOnRegisterDate"),
        "goods_services":    record.get("goodsAndServices"),
        "legal_owner":       legal_owner,
    }


# ============================================================
# Section 6 — Diagnostic helper (used by /api/debug/trademark-auth)
# ============================================================

def diagnose_token() -> Dict[str, Any]:
    """
    Test the OAuth token fetch in isolation and return a structured report.
    Called by GET /api/debug/trademark-auth in main.py.
    """
    client_id     = (os.getenv("IP_AUSTRALIA_CLIENT_ID")     or "").strip()
    client_secret = (os.getenv("IP_AUSTRALIA_CLIENT_SECRET") or "").strip()
    token_url_val = _token_url()
    tm_url_val    = _base_url()

    report: Dict[str, Any] = {
        "token_url":          token_url_val,
        "trademark_url":      tm_url_val,
        "client_id_set":      bool(client_id),
        "client_secret_set":  bool(client_secret),
        "token_obtained":     False,
        "method_used":        None,
        "http_status":        None,
        "error":              None,
    }

    if not client_id or not client_secret:
        report["error"] = (
            "IP_AUSTRALIA_CLIENT_ID or IP_AUSTRALIA_CLIENT_SECRET is missing from .env. "
            "Copy backend/.env.example to backend/.env and fill in your credentials."
        )
        return report

    basic_creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    # Try Method A: Basic Auth
    try:
        resp = requests.post(
            token_url_val,
            headers={
                "Content-Type":  "application/x-www-form-urlencoded",
                "Accept":        "application/json",
                "Authorization": f"Basic {basic_creds}",
            },
            data={"grant_type": "client_credentials"},
            timeout=20,
        )
        report["http_status"] = resp.status_code
        if resp.ok and resp.json().get("access_token"):
            report["token_obtained"] = True
            report["method_used"]    = "Basic Auth header"
            return report
        report["error"] = f"Basic Auth: HTTP {resp.status_code} — {resp.text[:200]}"
    except Exception as e:
        report["error"] = f"Basic Auth exception: {e}"

    # Try Method B: body params
    try:
        resp = requests.post(
            token_url_val,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
            timeout=20,
        )
        report["http_status"] = resp.status_code
        if resp.ok and resp.json().get("access_token"):
            report["token_obtained"] = True
            report["method_used"]    = "Body params"
            report["error"]          = None
            return report
        report["error"] = (
            report.get("error", "") +
            f" | Body params: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    except Exception as e:
        report["error"] = (report.get("error", "") or "") + f" | Body params exception: {e}"

    return report


# ============================================================
# Section 7 — Orchestrated Brand Resolution
# ============================================================

def resolve_brand(
    brand_name:    str,
    abr_lookup_fn: Any = None,
) -> Dict[str, Any]:
    """
    Full brand name resolution pipeline.
    1. Validate brand_name (minimum 2 characters).
    2. POST {TRADEMARK_URL}/search/quick  (WORD + REGISTERED) → trademarkIds.
    3. Walk IDs — GET {TRADEMARK_URL}/trade-mark/{id} until one resolves.
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
    pipeline.append(f"  token_url     : {_token_url()}")
    pipeline.append(f"  trademark_url : {_base_url()}")

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
# Section 8 — Convenience wrapper for main.py
# ============================================================

def run_brand_phase(
    brand_name:    str,
    abr_lookup_fn: Any = None,
) -> Dict[str, Any]:
    """Thin wrapper used by /api/search brand branch in main.py."""
    resolved = resolve_brand(brand_name, abr_lookup_fn=abr_lookup_fn)
    resolved["status"] = "external_resolved" if resolved["success"] else "not_found"
    return resolved
