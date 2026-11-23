"""LLM-based extractor (Claude or Gemini). Reads the PDF/image directly, including scans.

The LLM only *reads* the invoice. It never decides anything - all decisions are
deterministic rules in pipeline.py, so every outcome is explainable and auditable.
"""
import base64
import json
import os
import re

import httpx

_GEMINI_AUTO: str | None = None  # model picked automatically if the configured one is unavailable

PROMPT = """You are an accounts-payable data extraction engine. Read the attached vendor invoice and return ONLY a JSON object, no prose, with exactly these keys:

{
  "vendor_name": string|null,          // the SELLER issuing the invoice
  "vendor_gstin": string|null,         // seller's 15-char GSTIN
  "buyer_name": string|null,           // the BILL-TO party
  "buyer_gstin": string|null,
  "invoice_number": string|null,       // exactly as printed
  "invoice_date": "YYYY-MM-DD"|null,   // Indian invoices use day-first dates (12.11.2026 = 12 Nov 2026)
  "po_number": string|null,            // purchase order reference, only if printed
  "currency": string|null,
  "line_items": [{"description": string, "quantity": number, "unit_price": number, "amount": number}],
  "subtotal": number|null,             // taxable value before tax
  "tax_lines": [{"type": "IGST"|"CGST"|"SGST"|"UTGST"|"OTHER", "rate": number|null, "amount": number}],
  "tax_total": number|null,
  "total": number|null,                // final amount payable
  "bank_account": string|null,         // remit-to account number, digits only
  "ifsc": string|null,
  "bank_name": string|null,
  "notes": [string],                   // any remarks that matter for payment, e.g. bank detail changes
  "field_confidence": {"vendor_name": 0-1, "vendor_gstin": 0-1, "invoice_number": 0-1, "invoice_date": 0-1, "po_number": 0-1, "subtotal": 0-1, "total": 0-1, "line_items": 0-1, "bank_account": 0-1}
}

Rules: never guess or compute a value that is not printed - use null (missing data is a valid, important answer).
Numbers must be plain numbers without commas or currency symbols. Lower the confidence for anything hard to read."""


def provider() -> str | None:
    forced = os.getenv("EXTRACTOR", "auto").lower()
    if forced == "rules":
        return None
    if os.getenv("ANTHROPIC_API_KEY") and forced in ("auto", "claude"):
        return "claude"
    if os.getenv("GEMINI_API_KEY") and forced in ("auto", "gemini"):
        return "gemini"
    return None


def model_name(p: str) -> str:
    if p == "claude":
        return os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
    return _GEMINI_AUTO or os.getenv("GEMINI_MODEL", "gemini-3.6-flash")


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("model returned no JSON")
    return json.loads(m.group(0))


def _call_claude(data: bytes, mime: str) -> dict:
    b64 = base64.standard_b64encode(data).decode()
    block = ({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
             if mime == "application/pdf" else
             {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model_name("claude"), "max_tokens": 2000, "temperature": 0,
              "messages": [{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}]},
        timeout=40,
    )
    if r.status_code != 200:
        raise _api_error(r, "Claude")
    text = "".join(c.get("text", "") for c in r.json()["content"] if c.get("type") == "text")
    return _parse_json(text)


def _api_error(r: httpx.Response, who: str) -> RuntimeError:
    """Readable error with the HTTP status and the provider's own message (never includes the key)."""
    try:
        msg = r.json().get("error", {}).get("message", "")
    except Exception:
        msg = r.text[:200]
    return RuntimeError(f"{who} HTTP {r.status_code}: {msg[:180]}")


def _pick_gemini_model(key: str) -> str | None:
    """Ask the API which models this key can use and pick the newest stable general Flash model."""
    r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key, "pageSize": 200}, timeout=20)
    if r.status_code != 200:
        return None
    best, best_ver = None, ()
    for m in r.json().get("models", []):
        name = m.get("name", "").removeprefix("models/")
        if "generateContent" not in m.get("supportedGenerationMethods", []) or "flash" not in name:
            continue
        if any(t in name for t in ("lite", "image", "tts", "live", "audio", "transcribe", "exp", "thinking", "8b")):
            continue
        ver = tuple(int(n) for n in re.findall(r"\d+", name.split("flash")[0]))
        stable = "preview" not in name
        rank = (stable, ver)
        if best is None or rank > best_ver:
            best, best_ver = name, rank
    return best


def _call_gemini(data: bytes, mime: str) -> dict:
    global _GEMINI_AUTO
    key = os.environ["GEMINI_API_KEY"]
    body = {"contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
                                    {"text": PROMPT}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}

    def post(model):
        return httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          params={"key": key}, json=body, timeout=40)

    model = _GEMINI_AUTO or model_name("gemini")
    r = post(model)
    if r.status_code == 404 and not _GEMINI_AUTO:  # configured model retired / renamed -> discover one
        picked = _pick_gemini_model(key)
        if picked and picked != model:
            _GEMINI_AUTO, model = picked, picked
            r = post(model)
    if r.status_code != 200:
        raise _api_error(r, "Gemini")
    cand = (r.json().get("candidates") or [{}])[0]
    parts = cand.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text:
        raise RuntimeError(f"Gemini returned no text (finishReason {cand.get('finishReason')})")
    return _parse_json(text)


def normalise(d: dict) -> dict:
    """Coerce model output into the same shape the rules extractor produces."""
    def num(x):
        if x is None or x == "":
            return None
        try:
            return round(float(str(x).replace(",", "").replace("₹", "").replace("INR", "").strip()), 2)
        except ValueError:
            return None

    out = {k: d.get(k) for k in ["vendor_name", "vendor_gstin", "buyer_name", "buyer_gstin", "invoice_number",
                                 "invoice_date", "po_number", "currency", "bank_name"]}
    for k in ["vendor_gstin", "buyer_gstin"]:
        if out[k]:
            out[k] = re.sub(r"\s", "", str(out[k])).upper()
    out["currency"] = out["currency"] or "INR"
    out["subtotal"], out["tax_total"], out["total"] = num(d.get("subtotal")), num(d.get("tax_total")), num(d.get("total"))
    out["bank_account"] = re.sub(r"\D", "", str(d["bank_account"])) if d.get("bank_account") else None
    out["ifsc"] = str(d["ifsc"]).replace(" ", "").upper() if d.get("ifsc") else None
    if out["po_number"]:
        po = str(out["po_number"]).strip().upper()
        out["po_number"] = "PO-" + re.sub(r"\D", "", po) if re.fullmatch(r"PO[\s/\-]?\d+", po) else po
    out["line_items"] = [
        {"description": str(li.get("description", "")).strip(), "quantity": num(li.get("quantity")),
         "unit_price": num(li.get("unit_price")), "amount": num(li.get("amount"))}
        for li in (d.get("line_items") or [])
    ]
    out["tax_lines"] = [{"type": str(t.get("type", "OTHER")).upper(), "rate": num(t.get("rate")), "amount": num(t.get("amount"))}
                        for t in (d.get("tax_lines") or [])]
    if out["tax_total"] is None and out["tax_lines"]:
        out["tax_total"] = round(sum(t["amount"] or 0 for t in out["tax_lines"]), 2)
    out["notes"] = [str(n) for n in (d.get("notes") or [])]
    fc = d.get("field_confidence") or {}
    out["field_confidence"] = {k: float(fc.get(k, 0.9 if out.get(k) not in (None, [], "") else 0.0) or 0)
                               for k in ["vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_number",
                                         "subtotal", "total", "line_items", "bank_account"]}
    return out


def extract(data: bytes, mime: str) -> tuple[dict, str]:
    p = provider()
    if p is None:
        raise RuntimeError("no LLM provider configured")
    raw = _call_claude(data, mime) if p == "claude" else _call_gemini(data, mime)
    return normalise(raw), f"{p}:{model_name(p)}"
