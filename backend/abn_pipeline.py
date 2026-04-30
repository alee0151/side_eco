"""
abn_pipeline.py
===============

Purpose
-------
Handles both ABN number lookup and company name search for the EcoTrace pipeline.

This module owns all ABR (Australian Business Register) Web Services calls
so they are not scattered across main.py, barcode_pipeline.py, and brand_pipeline.py.

ABN pipeline flow
-----------------
  User input (11-digit ABN)
      │
      ├─── 1. Format check     ── digits only, length == 11
      ├─── 2. Checksum         ── ATO weighted-sum algorithm (mod 89)
      └─── 3. ABR lookup       ── SearchByABNv202001
                                   returns: legal name, entity type, ABN status,
                                            ACN, GST registration, state, postcode,
                                            main business activity

Company name pipeline flow
--------------------------
  User input (company / trading name string)
      │
      ├─── 1. Name validation  ── minimum 2 characters
      └─── 2. ABR name search  ── ABRSearchByNameAdvancedSimpleProtocol2017
                                   deduplicates by ABN
                                   prefers Active entities
                                   returns best_match + all_results

External APIs
-------------
ABR SearchByABN:
  GET https://abr.business.gov.au/abrxmlsearch/AbrXmlSearch.asmx/SearchByABNv202001

ABR SearchByName:
  GET https://abr.business.gov.au/abrxmlsearch/AbrXmlSearch.asmx/ABRSearchByNameAdvancedSimpleProtocol2017

Required .env variable: ABR_GUID
Obtain a free GUID from https://abr.business.gov.au/Tools/WebServices

Public exports
--------------
Functions used outside this module:

    run_abn_phase(abn)               -> full ABN lookup with checksum validation
    run_company_phase(company_name)  -> full company name search
    run_company_abn_phase(value)     -> auto-detects ABN vs name; used by main.py

    verify_abn_with_abr(abn)                  -> raw ABR ABN lookup (used by barcode/brand pipelines)
    search_company_name_with_abr(name)         -> raw ABR name search  (used as abr_lookup_fn)

    clean_abn(value)  -> strip whitespace from ABN string
    is_abn(value)     -> True if value is 11 digits after cleaning
    validate_abn_checksum(abn)  -> True if ATO checksum passes
"""

import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests


# ============================================================
# ABN Helpers
# ============================================================

def clean_abn(value: str) -> str:
    """Remove all whitespace from an ABN string.

    '12 345 678 901'  ->  '12345678901'
    """
    return re.sub(r"\s+", "", value or "")


def is_abn(value: str) -> bool:
    """Return True if the value is exactly 11 digits after whitespace removal.

    Does NOT validate the checksum — call validate_abn_checksum() for that.
    """
    cleaned = clean_abn(value)
    return cleaned.isdigit() and len(cleaned) == 11


def validate_abn_checksum(abn: str) -> bool:
    """
    Validate an ABN using the ATO weighted-sum checksum algorithm.

    Algorithm (from https://abr.business.gov.au/Help/AbnFormat):
    1. Subtract 1 from the first digit to produce an 11-digit working number.
    2. Multiply each digit by its positional weight:
       Weights: [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
    3. Sum all products.
    4. If the sum is divisible by 89 the ABN is valid.

    Returns True for a valid ABN, False otherwise.
    An ABN that fails is_abn() always returns False.
    """
    abn = clean_abn(abn)
    if not abn.isdigit() or len(abn) != 11:
        return False

    weights = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
    digits  = [int(d) for d in abn]
    digits[0] -= 1  # Step 1: subtract 1 from first digit

    total = sum(d * w for d, w in zip(digits, weights))
    return total % 89 == 0


# ============================================================
# ABR XML Namespace
# ============================================================

_ABR_NS = {"abr": "http://abr.business.gov.au/ABRXMLSearch/"}


def _text(node, path: str) -> Optional[str]:
    """Find a child node by XPath and return its text, or None if not found."""
    found = node.find(path, _ABR_NS)
    return found.text if found is not None else None


def _check_abr_exception(root) -> Optional[str]:
    """Return the ABR exception description if present, else None."""
    ex = root.find(".//abr:exception", _ABR_NS)
    if ex is not None:
        desc = ex.find(".//abr:exceptionDescription", _ABR_NS)
        return desc.text if desc is not None else "ABR returned an unspecified exception"
    return None


# ============================================================
# ABR ABN Lookup  (SearchByABNv202001)
# ============================================================

def verify_abn_with_abr(abn: str) -> Dict[str, Any]:
    """
    Verify a single ABN via ABR SearchByABNv202001.

    Returns a dict with the entity's legal name, entity type, ABN status,
    ACN, GST registration date, state, postcode and main business activity.

    Required .env: ABR_GUID

    Used by:
    - run_abn_phase()         (ABN branch of /api/search)
    - /api/abn/verify/:abn   (standalone endpoint in main.py)
    - barcode_pipeline.py    (indirectly via search_company_name_with_abr)
    """
    guid = os.getenv("ABR_GUID", "").strip()
    if not guid:
        return {"success": False, "source": "ABR", "message": "ABR_GUID missing in .env"}

    abn = clean_abn(abn)
    if not is_abn(abn):
        return {"success": False, "source": "ABR", "message": "ABN must be 11 digits"}

    url    = "https://abr.business.gov.au/abrxmlsearch/AbrXmlSearch.asmx/SearchByABNv202001"
    params = {
        "searchString":            abn,
        "includeHistoricalDetails": "N",
        "authenticationGuid":      guid,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except requests.exceptions.Timeout:
        return {"success": False, "source": "ABR", "message": "ABR request timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "source": "ABR", "message": f"Network error: {e}"}
    except ET.ParseError as e:
        return {"success": False, "source": "ABR", "message": f"Invalid XML from ABR: {e}"}

    # Check for ABR-level error first.
    err = _check_abr_exception(root)
    if err:
        return {"success": False, "source": "ABR", "message": err}

    # ABN node: identifierValue whose type is ABN.
    abn_node = root.find(".//abr:identifierValue", _ABR_NS)
    if abn_node is None:
        return {"success": False, "source": "ABR", "message": "ABN not found in ABR register"}

    # ----------------------------------------------------------------
    # Legal name — organisations use organisationName; individuals use
    # given/family name combination.
    # ----------------------------------------------------------------
    org_name   = _text(root, ".//abr:organisationName")
    given_name = _text(root, ".//abr:givenName")
    family_name = _text(root, ".//abr:familyName")
    legal_name = (
        org_name
        or " ".join(filter(None, [given_name, family_name]))
        or None
    )

    # ----------------------------------------------------------------
    # Entity type (e.g., "Australian Private Company", "Individual/Sole Trader")
    # ----------------------------------------------------------------
    entity_type_code = _text(root, ".//abr:entityTypeCode")
    entity_type_desc = _text(root, ".//abr:entityTypeDescription")
    if not entity_type_desc and entity_type_code:
        entity_type_desc = entity_type_code

    # ----------------------------------------------------------------
    # ACN (if the entity has one)
    # ----------------------------------------------------------------
    acn = None
    for id_node in root.findall(".//abr:identifier", _ABR_NS):
        id_type  = _text(id_node, ".//abr:identifierType")
        id_value = _text(id_node, ".//abr:identifierValue")
        if id_type and "acn" in id_type.lower() and id_value:
            acn = id_value
            break

    # ----------------------------------------------------------------
    # GST registration
    # ----------------------------------------------------------------
    gst_node         = root.find(".//abr:goodsAndServicesTax", _ABR_NS)
    gst_registered   = gst_node is not None
    gst_from_date    = _text(root, ".//abr:goodsAndServicesTax/abr:effectiveFrom") if gst_registered else None

    # ----------------------------------------------------------------
    # Main business address
    # ----------------------------------------------------------------
    state    = _text(root, ".//abr:mainBusinessPhysicalAddress/abr:stateCode") \
             or _text(root, ".//abr:stateCode")
    postcode = _text(root, ".//abr:mainBusinessPhysicalAddress/abr:postcode") \
             or _text(root, ".//abr:postcode")

    # ----------------------------------------------------------------
    # Main business activity (ANZSIC code + description)
    # ----------------------------------------------------------------
    anzsic_code = _text(root, ".//abr:mainBusinessActivity/abr:code")
    anzsic_desc = _text(root, ".//abr:mainBusinessActivity/abr:description")
    if anzsic_code and not anzsic_desc:
        anzsic_desc = anzsic_code

    # ----------------------------------------------------------------
    # ABN status
    # ----------------------------------------------------------------
    abn_status = (
        _text(root, ".//abr:entityStatus/abr:entityStatusCode")
        or _text(root, ".//abr:entityStatusCode")
    )

    return {
        "success":         True,
        "source":          "ABR",
        "abn":             abn_node.text,
        "legal_name":      legal_name,
        "entity_type":     entity_type_desc,
        "entity_type_code": entity_type_code,
        "acn":             acn,
        "state":           state,
        "postcode":        postcode,
        "abn_status":      abn_status,
        "gst_registered":  gst_registered,
        "gst_from_date":   gst_from_date,
        "main_activity":   anzsic_desc,
        "verified":        True,
    }


# ============================================================
# ABR Company Name Search  (ABRSearchByNameAdvancedSimpleProtocol2017)
# ============================================================

def search_company_name_with_abr(company_name: str) -> Dict[str, Any]:
    """
    Search ABR by company or business name.

    Returns the best match (Active entity with ABN preferred) plus all
    deduplicated results ordered by relevance.

    Used by:
    - run_company_phase()            (company name branch of /api/search)
    - barcode_pipeline.py            (passed in as abr_lookup_fn)
    - brand_pipeline.py              (passed in as abr_lookup_fn)
    - /api/company/search/:name     (standalone endpoint in main.py)

    Required .env: ABR_GUID
    """
    guid = os.getenv("ABR_GUID", "").strip()
    if not guid:
        return {"success": False, "source": "ABR", "message": "ABR_GUID missing in .env"}

    company_name = (company_name or "").strip()
    if len(company_name) < 2:
        return {"success": False, "source": "ABR", "message": "Company name must be at least 2 characters"}

    url    = "https://abr.business.gov.au/abrxmlsearch/AbrXmlSearch.asmx/ABRSearchByNameAdvancedSimpleProtocol2017"
    params = {
        "name":               company_name,
        "postcode":           "",
        "legalName":          "Y",
        "tradingName":        "Y",
        # Include all Australian states/territories.
        "NSW": "Y", "SA": "Y", "ACT": "Y", "VIC": "Y",
        "WA":  "Y", "NT":  "Y", "QLD": "Y", "TAS": "Y",
        "authenticationGuid": guid,
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except requests.exceptions.Timeout:
        return {"success": False, "source": "ABR", "message": "ABR request timed out"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "source": "ABR", "message": f"Network error: {e}"}
    except ET.ParseError as e:
        return {"success": False, "source": "ABR", "message": f"Invalid XML from ABR: {e}"}

    err = _check_abr_exception(root)
    if err:
        return {"success": False, "source": "ABR", "message": err}

    businesses = root.findall(".//abr:businessEntity", _ABR_NS)
    if not businesses:
        return {
            "success":     False,
            "source":      "ABR",
            "message":     f"No company found in ABR for '{company_name}'",
            "all_results": [],
            "total":       0,
        }

    results:  List[Dict] = []
    seen_abns: set       = set()

    for business in businesses:
        abn_node    = business.find(".//abr:identifierValue", _ABR_NS)
        state_node  = business.find(".//abr:stateCode", _ABR_NS)
        post_node   = business.find(".//abr:postcode",   _ABR_NS)
        status_node = business.find(".//abr:entityStatusCode", _ABR_NS)
        type_node   = business.find(".//abr:entityTypeDescription", _ABR_NS)

        abn_val = abn_node.text if abn_node is not None else None
        if abn_val in seen_abns:
            continue
        seen_abns.add(abn_val)

        # ----------------------------------------------------------------
        # Legal name: organisations use organisationName; individuals use
        # givenName + familyName.
        # ----------------------------------------------------------------
        name_node = business.find(".//abr:organisationName", _ABR_NS)
        if name_node is not None:
            legal_name = name_node.text
        else:
            given  = business.find(".//abr:givenName",  _ABR_NS)
            family = business.find(".//abr:familyName", _ABR_NS)
            legal_name = " ".join(
                filter(None, [
                    given.text  if given  is not None else None,
                    family.text if family is not None else None,
                ])
            ) or company_name

        results.append({
            "abn":         abn_val,
            "legal_name":  legal_name,
            "state":       state_node.text  if state_node  is not None else None,
            "postcode":    post_node.text   if post_node   is not None else None,
            "abn_status":  status_node.text if status_node is not None else None,
            "entity_type": type_node.text   if type_node   is not None else None,
            "verified":    abn_val is not None,
        })

    # Prefer the first Active entity with a verified ABN.
    # Fall back to the first result if none qualify.
    best = next(
        (r for r in results if r.get("abn_status") == "Active" and r["abn"]),
        results[0] if results else None,
    )

    if not best:
        return {
            "success":     False,
            "source":      "ABR",
            "message":     f"No company found in ABR for '{company_name}'",
            "all_results": results,
            "total":       len(results),
        }

    return {
        "success":     True,
        "source":      "ABR",
        **best,
        "best_match":  best,
        "all_results": results,
        "total":       len(results),
    }


# ============================================================
# Pipeline Runners
# ============================================================

def run_abn_phase(abn: str) -> Dict[str, Any]:
    """
    Full ABN input pipeline.

    Steps
    -----
    1. Format check  — must be 11 digits after whitespace removal
    2. Checksum      — ATO weighted-sum mod-89 algorithm
    3. ABR lookup    — legal name, entity type, ACN, GST, state, postcode

    Returns
    -------
    dict with:
        success        bool
        abn            str   cleaned 11-digit ABN
        valid_format   bool
        valid_checksum bool
        legal_name     str   from ABR (or None)
        entity_type    str   from ABR (or None)
        acn            str   from ABR (or None)
        abn_status     str   'Active' | 'Cancelled' | ... (from ABR)
        gst_registered bool
        state          str   from ABR
        postcode       str   from ABR
        main_activity  str   ANZSIC description (or None)
        source         str   'ABR'
        confidence     int   0-100
        pipeline       list  executed steps
        errors         list  non-fatal warnings
    """
    pipeline: List[str] = []
    errors:   List[str] = []

    abn_raw = abn
    abn     = clean_abn(abn)

    # ----------------------------------------------------------
    # Step 1 — Format check
    # ----------------------------------------------------------
    pipeline.append("ABN format check")
    valid_format = is_abn(abn)
    if not valid_format:
        return {
            "success":        False,
            "abn":            abn,
            "valid_format":   False,
            "valid_checksum": False,
            "error":          f"'{abn_raw}' is not a valid ABN format (must be 11 digits).",
            "pipeline":       pipeline,
            "errors":         errors,
            "confidence":     0,
        }

    # ----------------------------------------------------------
    # Step 2 — Checksum validation
    # ----------------------------------------------------------
    pipeline.append("ABN checksum validation (ATO mod-89)")
    valid_checksum = validate_abn_checksum(abn)
    if not valid_checksum:
        errors.append(
            f"ABN {abn} fails the ATO checksum — this ABN is not mathematically valid. "
            "The search will still proceed against ABR."
        )

    # ----------------------------------------------------------
    # Step 3 — ABR lookup
    # ----------------------------------------------------------
    pipeline.append(f"ABR ABN lookup: {abn}")
    abr = verify_abn_with_abr(abn)

    if not abr.get("success"):
        return {
            "success":        False,
            "abn":            abn,
            "valid_format":   valid_format,
            "valid_checksum": valid_checksum,
            "error":          abr.get("message", "ABR lookup failed"),
            "source":         "ABR",
            "pipeline":       pipeline,
            "errors":         errors,
            "confidence":     10 if valid_checksum else 0,
        }

    # ----------------------------------------------------------
    # Confidence scoring
    # ----------------------------------------------------------
    confidence = 0
    if valid_format:                                    confidence += 10
    if valid_checksum:                                  confidence += 20
    if abr.get("abn_status") == "Active":               confidence += 50
    elif abr.get("abn_status"):                         confidence += 20
    if abr.get("gst_registered"):                       confidence += 10
    if abr.get("legal_name"):                           confidence += 10

    return {
        "success":         True,
        "abn":             abn,
        "valid_format":    valid_format,
        "valid_checksum":  valid_checksum,
        "legal_name":      abr.get("legal_name"),
        "entity_type":     abr.get("entity_type"),
        "entity_type_code": abr.get("entity_type_code"),
        "acn":             abr.get("acn"),
        "state":           abr.get("state"),
        "postcode":        abr.get("postcode"),
        "abn_status":      abr.get("abn_status"),
        "gst_registered":  abr.get("gst_registered"),
        "gst_from_date":   abr.get("gst_from_date"),
        "main_activity":   abr.get("main_activity"),
        "source":          "ABR",
        "status":          "external_resolved" if abr.get("abn_status") == "Active" else "external_found",
        "confidence":      min(confidence, 100),
        "pipeline":        pipeline,
        "errors":          errors,
    }


def run_company_phase(company_name: str) -> Dict[str, Any]:
    """
    Full company name input pipeline.

    Steps
    -----
    1. Name validation  — minimum 2 characters
    2. ABR name search  — all Australian states, legal + trading names

    Returns
    -------
    dict with:
        success      bool
        company_name str   input as provided
        company      dict  best match: legal_name, abn, entity_type, state, postcode, abn_status
        all_results  list  all deduplicated ABR matches
        total        int   count of all matches
        source       str   'ABR'
        confidence   int   0-100
        pipeline     list  executed steps
        errors       list  non-fatal warnings
    """
    pipeline: List[str] = []
    errors:   List[str] = []

    company_name = (company_name or "").strip()

    # ----------------------------------------------------------
    # Step 1 — Validate
    # ----------------------------------------------------------
    pipeline.append("Company name validation")
    if len(company_name) < 2:
        return {
            "success":      False,
            "company_name": company_name,
            "error":        "Company name must be at least 2 characters.",
            "pipeline":     pipeline,
            "errors":       errors,
            "confidence":   0,
        }

    # ----------------------------------------------------------
    # Step 2 — ABR name search
    # ----------------------------------------------------------
    pipeline.append(f"ABR company name search: '{company_name}'")
    abr = search_company_name_with_abr(company_name)

    if not abr.get("success"):
        return {
            "success":      False,
            "company_name": company_name,
            "company":      None,
            "all_results":  abr.get("all_results", []),
            "total":        abr.get("total", 0),
            "source":       "ABR",
            "error":        abr.get("message", "ABR search failed"),
            "status":       "not_found",
            "pipeline":     pipeline,
            "errors":       errors,
            "confidence":   0,
        }

    best = abr.get("best_match", {})

    # ----------------------------------------------------------
    # Confidence scoring
    # ----------------------------------------------------------
    confidence = 0
    if best.get("abn"):                                 confidence += 30
    if best.get("abn_status") == "Active":              confidence += 40
    elif best.get("abn_status"):                        confidence += 15
    if best.get("legal_name"):                          confidence += 20
    if abr.get("total", 0) == 1:                        confidence += 10  # exact match

    return {
        "success":      True,
        "company_name": company_name,
        "company": {
            "legal_name":  best.get("legal_name"),
            "abn":         best.get("abn"),
            "entity_type": best.get("entity_type"),
            "state":       best.get("state"),
            "postcode":    best.get("postcode"),
            "abn_status":  best.get("abn_status"),
        },
        "all_results":  abr.get("all_results", []),
        "total":        abr.get("total", 0),
        "source":       "ABR",
        "status":       "external_resolved" if best.get("abn_status") == "Active" else "external_found",
        "confidence":   min(confidence, 100),
        "pipeline":     pipeline,
        "errors":       errors,
    }


def run_company_abn_phase(value: str) -> Dict[str, Any]:
    """
    Auto-detect whether the input is an ABN or a company name and dispatch
    to the appropriate pipeline.

    Used by the company_name branch in main.py /api/search.

    Detection rule:
    - If the value is exactly 11 digits (after whitespace removal) -> run_abn_phase()
    - Otherwise -> run_company_phase()

    This means inputs like '88 000 014 675' are treated as ABN,
    and inputs like 'Coles Group' are treated as company name.
    """
    cleaned = clean_abn(value)
    if cleaned.isdigit() and len(cleaned) == 11:
        return run_abn_phase(cleaned)
    return run_company_phase(value)
