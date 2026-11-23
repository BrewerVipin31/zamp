"""Deterministic (offline) invoice field extractor.

Works on the text layer of a PDF, or on OCR text for scans. It knows the common label
variants Indian vendors use ("Invoice No.", "Bill #", "Against PO No.", "Net Payable" ...).
It is the fallback when no LLM key is configured and the cross-check when one is.
"""
import re
from datetime import datetime

AMOUNT = r"(?:INR|Rs\.?|₹)?\s*([\d,]+\.\d{2})"
GSTIN_RE = re.compile(r"(?<![A-Z0-9])\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z](?![A-Z0-9])")
IFSC_RE = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")
BILL_TO_RE = re.compile(r"^\s*(bill(?:ed)?\s*to|buyer|to\s*,|ship\s*to|sold\s*to|customer\s*(?:name)?\s*:?\s*$)", re.I)
TITLE_WORDS = re.compile(r"^(tax\s+invoice|invoice|bill\s+of\s+supply|original|duplicate)\b", re.I)

INVOICE_NO_RE = re.compile(
    r"(?:invoice\s*(?:no\.?|number|num|#)|bill\s*(?:no\.?|number|#)|inv\.?\s*no\.?)\s*[:#.\-]?\s*(.+)$", re.I)
DATE_LABEL_RE = re.compile(r"(?:invoice\s*date|bill\s*date|dated|date)\s*[:\-]?\s*(.+)$", re.I)
PO_LABEL_RE = re.compile(
    r"(?:purchase\s*order(?:\s*(?:no\.?|number))?|customer\s*po|p\.?\s?o\.?\s*(?:no\.?|number|#)|against\s*po(?:\s*no\.?)?|your\s*po)"
    r"\s*[:#.\-]?\s*([A-Z0-9][A-Z0-9/\-]*\d)", re.I)
PO_ANY_RE = re.compile(r"\bPO[-/ ]?(\d{3,6})\b", re.I)
SUBTOTAL_RE = re.compile(
    r"^(sub\s*-?\s*total|taxable\s*(?:value|amount)|total\s*before\s*tax|amount\s*before\s*tax|total\s*taxable\s*value)\b.*?"
    + AMOUNT + r"\s*$", re.I)
TAX_RE = re.compile(r"\b(IGST|CGST|SGST|UTGST)\b\s*(?:@\s*)?(\d+(?:\.\d+)?)\s*%.*?" + AMOUNT + r"\s*$", re.I)
TOTAL_RE = re.compile(
    r"^(grand\s*total|total\s*invoice\s*value|invoice\s*total|amount\s*due|net\s*payable|total\s*amount\s*payable|"
    r"total\s*amount|balance\s*due|total\s*payable)\b.*?" + AMOUNT + r"\s*$", re.I)
LINE_RE = re.compile(
    r"^(?:\d{1,3}[.)]?\s+)?(?P<desc>.*?[A-Za-z].*?)\s+(?P<qty>\d[\d,]*(?:\.\d+)?)(?:\s*(?:nos|pcs|units?|ea|ream|box)\.?)?\s+"
    + r"(?:INR|Rs\.?|₹)?\s*(?P<rate>[\d,]+\.\d{2})\s+(?:INR|Rs\.?|₹)?\s*(?P<amt>[\d,]+\.\d{2})\s*$", re.I)
ACCOUNT_RE = re.compile(r"(?:a/?c\.?\s*(?:no\.?|number)?|account\s*(?:no\.?|number))\s*[:.]?\s*(\d[\d ]{6,22}\d)", re.I)
BANK_NAME_RE = re.compile(r"(?:bank\s*name|^bank|pay\s*to)\s*:\s*([^|\n]+?)(?:\s*\||$)", re.I)
BANK_CHANGE_RE = re.compile(r"(bank\s*details\s*(?:have\s*)?changed|new\s*bank\s*(?:account|details)|change\s*(?:of|in)\s*bank)", re.I)

DATE_FORMATS = ["%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%B %d, %Y", "%b %d, %Y",
                "%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%y", "%d-%b-%y"]


def money(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return round(float(s.replace(",", "").strip()), 2)
    except ValueError:
        return None


def parse_date(raw: str) -> str | None:
    raw = raw.strip().rstrip(".")
    candidates = [raw]
    m = re.search(r"(\d{1,2}[-/.]\w{2,9}[-/.]\d{2,4}|\w{3,9}\s+\d{1,2},\s*\d{4}|\d{1,2}\s+\w{3,9}\s+\d{4}|\d{4}-\d{2}-\d{2})", raw)
    if m:
        candidates.insert(0, m.group(1))
    for c in candidates:
        for f in DATE_FORMATS:
            try:
                return datetime.strptime(c.strip(), f).date().isoformat()
            except ValueError:
                continue
    return None


def _clean_id(value: str, ocr: bool) -> str | None:
    v = value.strip().split("  ")[0].strip(" .:-")
    if ocr:
        v = re.sub(r"\s+", "", v)  # OCR inserts stray spaces inside IDs ("KOS-261 1-042")
    else:
        v = v.split(" ")[0]
    return v if re.search(r"\d", v or "") else None


def extract(text: str, source: str = "text") -> dict:
    ocr = source == "ocr"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    upper_lines = [ln.upper() for ln in lines]
    out = {
        "vendor_name": None, "vendor_gstin": None, "buyer_name": None, "buyer_gstin": None,
        "invoice_number": None, "invoice_date": None, "po_number": None, "currency": "INR",
        "line_items": [], "subtotal": None, "tax_lines": [], "tax_total": None, "total": None,
        "bank_account": None, "ifsc": None, "bank_name": None, "notes": [],
    }

    # vendor name: first meaningful line at the top of the page
    for ln in lines[:6]:
        if TITLE_WORDS.match(ln) or not re.search(r"[A-Za-z]{3}", ln):
            continue
        out["vendor_name"] = ln.title() if ln.isupper() else ln
        break

    # GSTINs: before the bill-to block -> vendor; first after -> buyer
    bill_to_idx = next((i for i, ln in enumerate(lines) if BILL_TO_RE.match(ln)), None)
    for i, ul in enumerate(upper_lines):
        for g in GSTIN_RE.findall(ul.replace(" ", "")) or GSTIN_RE.findall(ul):
            if bill_to_idx is not None and i > bill_to_idx:
                if out["buyer_gstin"] is None:
                    out["buyer_gstin"] = g
            elif out["vendor_gstin"] is None:
                out["vendor_gstin"] = g
    if bill_to_idx is not None and bill_to_idx + 1 < len(lines):
        out["buyer_name"] = lines[bill_to_idx + 1]

    for ln in lines:
        if out["invoice_number"] is None and (m := INVOICE_NO_RE.search(ln)) and not re.search(r"date", ln, re.I):
            out["invoice_number"] = _clean_id(m.group(1), ocr)
        if out["po_number"] is None and (m := PO_LABEL_RE.search(ln)):
            out["po_number"] = m.group(1).upper()
        if BANK_CHANGE_RE.search(ln):
            out["notes"].append(ln)

    # date: prefer explicit "invoice date" label, never "due date"
    date_lines = sorted((ln for ln in lines if DATE_LABEL_RE.search(ln) and not re.search(r"\bdue\b|po\s*date", ln, re.I)),
                        key=lambda s: 0 if re.search(r"invoice\s*date|bill\s*date", s, re.I) else 1)
    for ln in date_lines:
        d = parse_date(DATE_LABEL_RE.search(ln).group(1))
        if d:
            out["invoice_date"] = d
            break

    if out["po_number"] is None and (m := PO_ANY_RE.search(text)):
        out["po_number"] = f"PO-{m.group(1)}"
    if out["po_number"] and re.fullmatch(r"PO[ /]?\d+", out["po_number"]):
        out["po_number"] = "PO-" + re.sub(r"\D", "", out["po_number"])

    for ln in lines:
        if (m := LINE_RE.match(ln)) and not SUBTOTAL_RE.match(ln) and not TOTAL_RE.match(ln) and not TAX_RE.search(ln):
            qty, rate, amt = money(m.group("qty")), money(m.group("rate")), money(m.group("amt"))
            if qty and rate is not None and amt is not None and abs(qty * rate - amt) <= max(1.0, 0.005 * amt):
                desc = re.sub(r"^\d{1,3}[.)]\s*", "", m.group("desc")).strip(" -|")
                out["line_items"].append({"description": desc, "quantity": qty, "unit_price": rate, "amount": amt})
            continue
        if out["subtotal"] is None and (m := SUBTOTAL_RE.match(ln)):
            out["subtotal"] = money(m.group(2))
            continue
        if (m := TAX_RE.search(ln)):
            out["tax_lines"].append({"type": m.group(1).upper(), "rate": float(m.group(2)), "amount": money(m.group(3))})
            continue
        if out["total"] is None and (m := TOTAL_RE.match(ln)):
            out["total"] = money(m.group(2))

    if out["tax_lines"]:
        out["tax_total"] = round(sum(t["amount"] or 0 for t in out["tax_lines"]), 2)

    for ln in lines:
        if out["bank_account"] is None and (m := ACCOUNT_RE.search(ln)):
            out["bank_account"] = re.sub(r"\s", "", m.group(1))
        if out["ifsc"] is None and (m := IFSC_RE.search(ln.upper())):
            out["ifsc"] = m.group(1)
        if out["bank_name"] is None and (m := BANK_NAME_RE.search(ln)):
            out["bank_name"] = m.group(1).strip()

    base = 0.75 if ocr else 0.9
    conf = {k: (base if out.get(k) not in (None, [], "") else 0.0)
            for k in ["vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_number", "subtotal", "total",
                      "line_items", "bank_account"]}
    out["field_confidence"] = conf
    return out
