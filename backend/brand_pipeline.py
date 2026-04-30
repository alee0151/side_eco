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
      ├─── 1. Validate       minimum length, strip whitespace
      │
      ├─── 2. OAuth token    POST to TEST token endpoint
      │                     Credentials: IP_AUSTRALIA_TEST_CLIENT_ID / SECRET
      │                     URL: test.api.ipaustralia.gov.au  (override via IP_AUSTRALIA_TOKEN_URL)
      │                     Method: Basic Auth header first, body params fallback
      │
      ├─── 3. TM Quick Search  POST to PRODUCTION API
      │                     URL: production.api.ipaustralia.gov.au  (override via IP_AUSTRALIA_TRADEMARK_URL)
      │                     Auth: Bearer token obtained in step 2
      │                     Filters: quickSearchType=WORD, status=REGISTERED
      │
      ├─── 4. TM Detail      GET from PRODUCTION API
      │                     Walks IDs until one resolves
      │
      ├─── 5. Owner pick     extract from record.owner[] (ApiPartyType[])
      │
      └─── 6. ABR verify     ABR name search using extracted legal owner

Environment split (fixed)
--------------------------
  Token endpoint   →  TEST  environment
  TM Search API    →  PRODUCTION environment

Environment variables  (set in backend/.env)
--------------------------------------------
  IP_AUSTRALIA_TEST_CLIENT_ID      TEST subscription client id       (required)
  IP_AUSTRALIA_TEST_CLIENT_SECRET  TEST subscription client secret   (required)

  IP_AUSTRALIA_TOKEN_URL      override token endpoint URL    (optional, leave blank)
  IP_AUSTRALIA_TRADEMARK_URL  override TM API base URL       (optional, leave blank)

Default URLs
------------
  Token     : https://test.api.ipaustralia.gov.au/public/external-token-api/v1/access_token
  TM API    : https://production.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1

OAuth notes
-----------
MuleSoft OAuth servers expect credentials in an HTTP Basic Auth header:

    Authorization: Basic base64(client_id:client_secret)
    Content-Type: application/x-www-form-urlencoded
    Body: grant_type=client_credentials

This implementation tries Basic Auth first, then falls back to body params.

Diagnostic
----------
    GET /api/debug/trademark-auth
"""

import base64
import os
import time
from typing import Any, Dict, List, Optional

import requests


# ============================================================
# Section 1 — URL helpers
# ============================================================

_DEFAULT_TOKEN_URL    = "https://test.api.ipaustralia.gov.au/public/external-token-api/v1/access_token"
_DEFAULT_TRADEMARK_URL = "https://production.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1"


def _token_url() -> str:
    """
    OAuth token endpoint.
    Always points to the TEST environment.
    Override via IP_AUSTRALIA_TOKEN_URL if needed.
    """
    override = (os.getenv("IP_AUSTRALIA_TOKEN_URL") or "").strip()
    return override if override else _DEFAULT_TOKEN_URL


def _base_url() -> str:
    """
    Trade Mark Search API base URL.
    Always points to the PRODUCTION environment.
    Override via IP_AUSTRALIA_TRADEMARK_URL if needed.
    """
    override = (os.getenv("IP_AUSTRALIA_TRADEMARK_URL") or "").strip()
    return override.rstrip("/") if override else _DEFAULT_TRADEMARK_URL


def _get_test_credentials():
    """Read TEST subscription credentials from .env."""
    client_id     = (os.getenv("IP_AUSTRALIA_TEST_CLIENT_ID")     or "").strip()
    client_secret = (os.getenv("IP_AUSTRALIA_TEST_CLIENT_SECRET") or "").strip()
    return client_id, client_secret


# ============================================================
# Section 2 — OAuth Token Management
# ============================================================

_TOKEN_CACHE: Dict[str, Any] = {
    "access_token": None,
    "expires_at":   0,
}


def _make_token_request(client_id: str, client_secret: str) -> Optional[requests.Response]:
    """
    Attempt to obtain an OAuth token using two methods:

    Method A (preferred) — HTTP Basic Auth header  (required by MuleSoft)
    Method B (fallback)  — credentials in POST body (RFC 6749 §2.3.1)
    """
    url         = _token_url()
    basic_creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    # Method A: Basic Auth header
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

    # Method B: credentials in body
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
    Token is always fetched from the TEST environment using TEST credentials.
    Cached in-process; refreshed 60 s before expiry.
    """
    now = int(time.time())
    if _TOKEN_CACHE["access_token"] and now < int(_TOKEN_CACHE["expires_at"]) - 60:
        return _TOKEN_CACHE["access_token"]

    client_id, client_secret = _get_test_credentials()

    if not client_id or not client_secret:
        print(
            "[brand_pipeline] ERROR: IP_AUSTRALIA_TEST_CLIENT_ID or "
            "IP_AUSTRALIA_TEST_CLIENT_SECRET is missing from .env.\n"
            "  Copy backend/.env.example to backend/.env and fill in your "
            "TEST subscription credentials from api.ipaustralia.gov.au."
        )
        return None

    print(f"[brand_pipeline] Fetching OAuth token (TEST) from: {_token_url()}")
    resp = _make_token_request(client_id, client_secret)

    if resp is None:
        print("[brand_pipeline] All token request attempts failed (network / timeout).")
        return None

    if not resp.ok:
        print(
            f"[brand_pipeline] Token endpoint returned HTTP {resp.status_code}.\n"
            f"  URL  : {_token_url()}\n"
            f"  Body : {resp.text[:300]}\n"
            "  Verify IP_AUSTRALIA_TEST_CLIENT_ID and IP_AUSTRALIA_TEST_CLIENT_SECRET "
            "are correct and the Sandbox subscription is active on the portal."
        )
        return None

    token_data   = resp.json()
    access_token = token_data.get("access_token")
    expires_in   = int(token_data.get("expires_in", 3600))

    if not access_token:
        print(f"[brand_pipeline] Token response had no access_token: {token_data}")
        return None

    _TOKEN_CACHE["access_token"] = access_token
    _TOKEN_CACHE["expires_at"]   = now + expires_in
    print(f"[brand_pipeline] Token obtained (TEST creds), expires in {expires_in}s.")
    return access_token


def _get_auth_headers() -> Optional[Dict[str, str]]:
    """Build Bearer authorisation headers for PRODUCTION API calls."""
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
# Section 3 — Quick Search (PRODUCTION API)
# ============================================================

def _quick_search(brand_name: str) -> Dict[str, Any]:
    """
    POST {PRODUCTION_TRADEMARK_URL}/search/quick
    Filters: WORD + REGISTERED
    Auth: Bearer token from TEST token endpoint.
    """
    headers = _get_auth_headers()
    if not headers:
        return {
            "success": False,
            "message": (
                "Unable to obtain IP Australia OAuth token. "
                "Check IP_AUSTRALIA_TEST_CLIENT_ID and IP_AUSTRALIA_TEST_CLIENT_SECRET "
                f"in backend/.env.  Token URL: {_token_url()}"
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
    print(f"[brand_pipeline] Quick search POST (PRODUCTION): {url}  query={brand_name!r}")

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
        return {"success": False, "message": "IP Australia quick search timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "message": f"IP Australia network error: {e}"}


# ============================================================
# Section 4 — Trademark Detail (PRODUCTION API)
# ============================================================

_TM_DETAIL_MAX_TRIES = 10


def _fetch_trademark_detail(trademark_id: str) -> Dict[str, Any]:
    """
    GET {PRODUCTION_TRADEMARK_URL}/trade-mark/{ipRightIdentifier}
    Auth: Bearer token from TEST token endpoint.
    """
    headers = _get_auth_headers()
    if not headers:
        return {"success": False, "message": "No auth token for trademark detail lookup"}

    url = f"{_base_url()}/trade-mark/{trademark_id}"
    print(f"[brand_pipeline] Detail GET (PRODUCTION): {url}")

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
            return {"success": False, "status_code": resp.status_code, "message": resp.text[:300]}

        return {"success": True, "data": resp.json()}

    except requests.exceptions.Timeout:
        return {"success": False, "message": "Trademark detail request timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "message": f"Trademark detail network error: {e}"}


def _fetch_first_available_trademark(
    ids: List[str],
    max_tries: int = _TM_DETAIL_MAX_TRIES,
) -> Dict[str, Any]:
    skipped: List[str] = []
    for tm_id in [str(i) for i in ids[:max_tries]]:
        resp = _fetch_trademark_detail(tm_id)
        if resp.get("success"):
            if skipped:
                print(f"[brand_pipeline] skipped 404 IDs {skipped}; resolved on {tm_id}")
            return {"success": True, "data": resp["data"], "tried": len(skipped) + 1, "id": tm_id}
        if resp.get("not_found"):
            skipped.append(tm_id)
            continue
        return {"success": False, "message": resp.get("message", "Trademark detail lookup failed"),
                "tried": len(skipped) + 1}
    return {
        "success": False,
        "message": f"None of the first {max_tries} trademark IDs returned a valid record. IDs tried: {skipped}",
        "tried": len(skipped),
    }


# ============================================================
# Section 5 — Owner Extraction
# ============================================================

def _extract_owner_from_record(record: Dict[str, Any]) -> Optional[str]:
    if not isinstance(record, dict):
        return None
    for key in ("owner", "addressForService"):
        lst = record.get(key)
        if isinstance(lst, list) and lst:
            first = lst[0]
            name  = first.get("name") if isinstance(first, dict) else first
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _build_trademark_summary(record: Dict[str, Any], legal_owner: Optional[str]) -> Dict:
    words     = record.get("words") or []
    word_mark = " ".join(words).strip() if isinstance(words, list) else None
    return {
        "number":            record.get("number"),
        "word_mark":         word_mark or None,
        "status":            record.get("statusCode") or record.get("statusGroup"),
        "filing_date":       record.get("filingDate") or record.get("lodgementDate"),
        "registration_date": record.get("enteredOnRegisterDate"),
        "goods_services":    record.get("goodsAndServices"),
        "legal_owner":       legal_owner,
    }


# ============================================================
# Section 6 — Diagnostic  (GET /api/debug/trademark-auth)
# ============================================================

def diagnose_token() -> Dict[str, Any]:
    """
    Test the OAuth token fetch in isolation.
    Reports credential presence, URLs in use, which auth method succeeded.
    """
    client_id, client_secret = _get_test_credentials()
    token_url_val            = _token_url()
    tm_url_val               = _base_url()

    report: Dict[str, Any] = {
        "token_environment":      "test  (fixed)",
        "trademark_environment":  "production  (fixed)",
        "token_url":              token_url_val,
        "trademark_url":          tm_url_val,
        "credential_vars": {
            "id":     "IP_AUSTRALIA_TEST_CLIENT_ID",
            "secret": "IP_AUSTRALIA_TEST_CLIENT_SECRET",
        },
        "client_id_set":     bool(client_id),
        "client_secret_set": bool(client_secret),
        "token_obtained":    False,
        "method_used":       None,
        "http_status":       None,
        "error":             None,
    }

    if not client_id or not client_secret:
        report["error"] = (
            "IP_AUSTRALIA_TEST_CLIENT_ID or IP_AUSTRALIA_TEST_CLIENT_SECRET "
            "is missing from backend/.env."
        )
        return report

    basic_creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    # Method A: Basic Auth
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

    # Method B: body params
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
            (report.get("error") or "") +
            f" | Body params: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    except Exception as e:
        report["error"] = (report.get("error") or "") + f" | Body params exception: {e}"

    return report


# ============================================================
# Section 7 — Orchestrated Brand Resolution
# ============================================================

def resolve_brand(
    brand_name:    str,
    abr_lookup_fn: Any = None,
) -> Dict[str, Any]:
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

    pipeline.append(f"OAuth token  ←  TEST  ({_token_url()})")
    pipeline.append(f"TM Search    →  PRODUCTION  ({_base_url()})")
    pipeline.append(f"Searching for: '{brand_name}'")

    search_resp = _quick_search(brand_name)

    if not search_resp.get("success"):
        msg = search_resp.get("message", "IP Australia search failed")
        return {
            "success": False, "brand_name": brand_name, "error": msg,
            "trademark": None, "legal_owner": None, "abr": None,
            "pipeline": pipeline, "errors": [msg], "confidence": 0,
        }

    search_data = search_resp["data"]
    total_ids   = search_data.get("count", 0)
    ids         = search_data.get("trademarkIds") or []

    if not ids:
        return {
            "success": False, "brand_name": brand_name,
            "error":   f"No registered word-mark trademarks found for '{brand_name}'",
            "trademark": None, "legal_owner": None, "abr": None,
            "pipeline": pipeline, "errors": errors, "confidence": 0,
        }

    pipeline.append(f"Fetch trademark detail (up to {_TM_DETAIL_MAX_TRIES} tries) from {total_ids} results")
    detail_resp = _fetch_first_available_trademark(ids)

    if not detail_resp.get("success"):
        msg = detail_resp.get("message", "Could not fetch any trademark detail record")
        errors.append(msg)
        return {
            "success": False, "brand_name": brand_name, "error": msg,
            "trademark": None, "legal_owner": None, "abr": None,
            "tm_total": total_ids, "pipeline": pipeline, "errors": errors, "confidence": 10,
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
    if resolved_id:                                                    confidence += 20
    if str(trademark.get("status") or "").lower() == "registered":    confidence += 30
    elif trademark.get("status"):                                      confidence += 10
    if legal_owner:                                                    confidence += 20
    if abr_result and abr_result.get("success"):                       confidence += 30

    return {
        "success": True, "brand_name": brand_name,
        "trademark": trademark, "legal_owner": legal_owner, "abr": abr_result,
        "tm_total": total_ids, "confidence": min(confidence, 100),
        "pipeline": pipeline, "errors": errors,
    }


def _strip_legal_suffix(name: str) -> str:
    import re
    for p in [r"\bPty\.?\s*Ltd\.?", r"\bPty\.?", r"\bLtd\.?", r"\bLimited\b",
              r"\bInc\.?", r"\bCorp\.?", r"\bLLC\.?", r"\bCo\.?", r"\bHoldings\b"]:
        name = re.sub(p, "", name, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", name).strip()


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
