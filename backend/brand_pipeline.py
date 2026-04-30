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
      │                         Credentials selected by IP_AUSTRALIA_ENV:
      │                           'test'       -> TEST_CLIENT_ID / TEST_CLIENT_SECRET
      │                           'production' -> PROD_CLIENT_ID / PROD_CLIENT_SECRET
      │                         Method: Basic Auth header first, body params fallback
      │
      ├─── 3. TM Search    ─── POST /search/quick
      │                         filters: quickSearchType=WORD, status=REGISTERED
      │
      ├─── 4. TM Detail    ─── GET /trade-mark/{ipRightIdentifier}
      │                         walks IDs until one resolves
      │
      ├─── 5. Owner pick   ─── extract from record.owner[] (ApiPartyType[])
      │
      └─── 6. ABR verify   ─── ABR name search using extracted legal owner

Environment variables  (set in backend/.env)
--------------------------------------------
  IP_AUSTRALIA_ENV             'test' (default) or 'production'

  IP_AUSTRALIA_TEST_CLIENT_ID      sandbox/test OAuth client id
  IP_AUSTRALIA_TEST_CLIENT_SECRET  sandbox/test OAuth client secret

  IP_AUSTRALIA_PROD_CLIENT_ID      production OAuth client id
  IP_AUSTRALIA_PROD_CLIENT_SECRET  production OAuth client secret

  IP_AUSTRALIA_TOKEN_URL       override token endpoint (optional)
  IP_AUSTRALIA_TRADEMARK_URL   override TM API base URL (optional)

Default URLs per environment
-----------------------------
  test        token : https://test.api.ipaustralia.gov.au/public/external-token-api/v1/access_token
  test        api   : https://test.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1
  production  token : https://production.api.ipaustralia.gov.au/public/external-token-api/v1/access_token
  production  api   : https://production.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1

OAuth notes
-----------
IP Australia uses a MuleSoft gateway. MuleSoft OAuth servers expect the
client credentials in an HTTP Basic Authentication header:

    Authorization: Basic base64(client_id:client_secret)
    Content-Type: application/x-www-form-urlencoded
    Body: grant_type=client_credentials

This implementation tries Basic Auth first and falls back to body params
(RFC 6749 §2.3.1) if the server returns 401 or 400.

Test with the diagnostic endpoint before running a full search:
    GET /api/debug/trademark-auth
"""

import base64
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


# ============================================================
# Section 1 — Environment / URL helpers
# ============================================================

_DEFAULT_URLS = {
    "test": {
        "token":     "https://test.api.ipaustralia.gov.au/public/external-token-api/v1/access_token",
        "trademark": "https://test.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1",
    },
    "production": {
        "token":     "https://production.api.ipaustralia.gov.au/public/external-token-api/v1/access_token",
        "trademark": "https://production.api.ipaustralia.gov.au/public/australian-trade-mark-search-api/v1",
    },
}


def _active_env() -> str:
    """Return 'test' or 'production' based on IP_AUSTRALIA_ENV (default: 'test')."""
    raw = (os.getenv("IP_AUSTRALIA_ENV") or "test").strip().lower()
    return "production" if raw == "production" else "test"


def _active_credentials() -> Tuple[str, str]:
    """
    Return (client_id, client_secret) for the currently active environment.

    test        -> IP_AUSTRALIA_TEST_CLIENT_ID / IP_AUSTRALIA_TEST_CLIENT_SECRET
    production  -> IP_AUSTRALIA_PROD_CLIENT_ID / IP_AUSTRALIA_PROD_CLIENT_SECRET
    """
    env = _active_env()
    if env == "production":
        client_id     = (os.getenv("IP_AUSTRALIA_PROD_CLIENT_ID")     or "").strip()
        client_secret = (os.getenv("IP_AUSTRALIA_PROD_CLIENT_SECRET") or "").strip()
    else:
        client_id     = (os.getenv("IP_AUSTRALIA_TEST_CLIENT_ID")     or "").strip()
        client_secret = (os.getenv("IP_AUSTRALIA_TEST_CLIENT_SECRET") or "").strip()
    return client_id, client_secret


def _token_url() -> str:
    """OAuth token endpoint. Override via IP_AUSTRALIA_TOKEN_URL; else env default."""
    override = (os.getenv("IP_AUSTRALIA_TOKEN_URL") or "").strip()
    if override:
        return override
    return _DEFAULT_URLS[_active_env()]["token"]


def _base_url() -> str:
    """Trade Mark Search API base URL. Override via IP_AUSTRALIA_TRADEMARK_URL; else env default."""
    override = (os.getenv("IP_AUSTRALIA_TRADEMARK_URL") or "").strip()
    if override:
        return override.rstrip("/")
    return _DEFAULT_URLS[_active_env()]["trademark"]


# ============================================================
# Section 2 — OAuth Token Management
# ============================================================

# In-process token cache keyed by (env, client_id) so switching envs
# does not serve a stale token from the other environment.
_TOKEN_CACHE: Dict[str, Any] = {}


def _cache_key() -> str:
    env, (cid, _) = _active_env(), _active_credentials()
    return f"{env}:{cid}"


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
    Obtain (or return cached) IP Australia OAuth token for the active environment.
    Cache is keyed by (env, client_id) to prevent cross-environment token reuse.
    Token is refreshed 60 s before expiry.
    """
    now        = int(time.time())
    cache_key  = _cache_key()
    cached     = _TOKEN_CACHE.get(cache_key, {})

    if cached.get("access_token") and now < int(cached.get("expires_at", 0)) - 60:
        return cached["access_token"]

    client_id, client_secret = _active_credentials()
    env                      = _active_env()

    if not client_id or not client_secret:
        missing_var = (
            f"IP_AUSTRALIA_{'PROD' if env == 'production' else 'TEST'}_CLIENT_ID"
            " or "
            f"IP_AUSTRALIA_{'PROD' if env == 'production' else 'TEST'}_CLIENT_SECRET"
        )
        print(
            f"[brand_pipeline] ERROR: {missing_var} is missing from .env.\n"
            "  Copy backend/.env.example to backend/.env and fill in your credentials.\n"
            f"  Current IP_AUSTRALIA_ENV = '{env}'"
        )
        return None

    print(f"[brand_pipeline] Fetching OAuth token ({env}) from: {_token_url()}")
    resp = _make_token_request(client_id, client_secret)

    if resp is None:
        print("[brand_pipeline] All token request attempts failed (network / timeout).")
        return None

    if not resp.ok:
        print(
            f"[brand_pipeline] Token endpoint returned HTTP {resp.status_code}. ({env})\n"
            f"  URL  : {_token_url()}\n"
            f"  Body : {resp.text[:300]}\n"
            "  Verify credentials are correct and the app subscription is active."
        )
        return None

    token_data   = resp.json()
    access_token = token_data.get("access_token")
    expires_in   = int(token_data.get("expires_in", 3600))

    if not access_token:
        print(f"[brand_pipeline] Token response had no access_token: {token_data}")
        return None

    _TOKEN_CACHE[cache_key] = {
        "access_token": access_token,
        "expires_at":   now + expires_in,
    }
    print(f"[brand_pipeline] Token obtained ({env}), expires in {expires_in}s.")
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
    POST {TRADEMARK_URL}/search/quick
    Filters: WORD + REGISTERED
    """
    headers = _get_auth_headers()
    if not headers:
        env = _active_env()
        var = f"IP_AUSTRALIA_{'PROD' if env == 'production' else 'TEST'}_CLIENT_"
        return {
            "success": False,
            "message": (
                f"Unable to obtain IP Australia OAuth token ({env} environment). "
                f"Check {var}ID and {var}SECRET in backend/.env. "
                f"Token URL: {_token_url()}"
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
            env = _active_env()
            _TOKEN_CACHE.pop(_cache_key(), None)
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
    headers = _get_auth_headers()
    if not headers:
        return {"success": False, "message": "No auth token for trademark detail lookup"}

    url = f"{_base_url()}/trade-mark/{trademark_id}"
    print(f"[brand_pipeline] Detail GET: {url}")

    try:
        resp = requests.get(url, headers=headers, timeout=20)

        if resp.status_code == 401:
            _TOKEN_CACHE.pop(_cache_key(), None)
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


_TM_DETAIL_MAX_TRIES = 10


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
    owner_list = record.get("owner")
    if isinstance(owner_list, list) and owner_list:
        first = owner_list[0]
        if isinstance(first, dict):
            name = first.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        elif isinstance(first, str) and first.strip():
            return first.strip()
    afs_list = record.get("addressForService")
    if isinstance(afs_list, list) and afs_list:
        first = afs_list[0]
        if isinstance(first, dict):
            name = first.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _build_trademark_summary(record: Dict[str, Any], legal_owner: Optional[str]) -> Dict:
    words     = record.get("words") or []
    word_mark = " ".join(words).strip() if isinstance(words, list) else None
    status    = record.get("statusCode") or record.get("statusGroup")
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
# Section 6 — Diagnostic helper  (used by /api/debug/trademark-auth)
# ============================================================

def diagnose_token() -> Dict[str, Any]:
    """
    Test the OAuth token fetch in isolation and return a structured report.
    Reports which credential pair is active, which method succeeded, and
    what HTTP status the token endpoint returned.
    """
    env                      = _active_env()
    client_id, client_secret = _active_credentials()
    token_url_val            = _token_url()
    tm_url_val               = _base_url()

    id_var  = f"IP_AUSTRALIA_{'PROD' if env == 'production' else 'TEST'}_CLIENT_ID"
    sec_var = f"IP_AUSTRALIA_{'PROD' if env == 'production' else 'TEST'}_CLIENT_SECRET"

    report: Dict[str, Any] = {
        "active_environment":  env,
        "credential_id_var":   id_var,
        "credential_sec_var":  sec_var,
        "token_url":           token_url_val,
        "trademark_url":       tm_url_val,
        "client_id_set":       bool(client_id),
        "client_secret_set":   bool(client_secret),
        "token_obtained":      False,
        "method_used":         None,
        "http_status":         None,
        "error":               None,
    }

    if not client_id or not client_secret:
        report["error"] = (
            f"{id_var} or {sec_var} is missing from .env. "
            "Copy backend/.env.example to backend/.env and fill in your credentials."
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
    env        = _active_env()

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

    pipeline.append(f"IP Australia quick search ({env}) (WORD+REGISTERED): '{brand_name}'")
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
    if resolved_id:                                                       confidence += 20
    if str(trademark.get("status") or "").lower() == "registered":       confidence += 30
    elif trademark.get("status"):                                         confidence += 10
    if legal_owner:                                                       confidence += 20
    if abr_result and abr_result.get("success"):                          confidence += 30

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
    import re
    patterns = [r"\bPty\.?\s*Ltd\.?", r"\bPty\.?", r"\bLtd\.?", r"\bLimited\b",
                r"\bInc\.?", r"\bCorp\.?", r"\bLLC\.?", r"\bCo\.?", r"\bHoldings\b"]
    result = name
    for p in patterns:
        result = __import__('re').sub(p, "", result, flags=__import__('re').IGNORECASE)
    return __import__('re').sub(r"\s+", " ", result).strip()


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
