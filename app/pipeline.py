"""The invoice-to-decision pipeline.

Stages: intake -> extraction -> data quality -> vendor verification -> duplicate check -> PO match -> decision.
Every stage returns checks with a severity. The decision is a pure function of those checks:
any REJECT -> REJECTED, else any HOLD -> ON HOLD, else APPROVED.
"""
import hashlib
import io
import os
import re
import time
from datetime import date, datetime

import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image
from rapidfuzz import fuzz

from . import extract_llm, extract_rules, gstin, store

SEVERITY = {"info": 0, "pass": 1, "warn": 2, "hold": 3, "reject": 4}
STAGES = [
    ("intake", "Intake", "Receive file, fingerprint it, detect text layer vs scan"),
    ("extract", "Extraction", "Read invoice fields (LLM or OCR + rules), cross-check engines"),
    ("quality", "Data quality", "Mandatory fields, arithmetic, dates, GSTIN validity, addressed to us"),
    ("vendor", "Vendor verification", "Match vendor master, status, GSTIN, bank account, tax type"),
    ("duplicate", "Duplicate check", "Same file, same invoice number (normalised), same amount + date"),
    ("po_match", "PO matching", "Find PO (explicit or inferred), match lines, price, qty, balance, tax rate"),
    ("decision", "Decision", "Combine findings into a decision, payable amount, routing and messages"),
]
PACING = int(os.getenv("DEMO_PACING_MS", "450")) / 1000


def inr(x) -> str:
    if x is None:
        return "-"
    neg = x < 0
    whole, frac = f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return ("-" if neg else "") + "₹" + whole + ("" if frac == "00" else "." + frac)


def canon_invoice_no(s: str | None) -> str | None:
    """'NCS-INV-2026-0117' and 'NCS/INV/2026/117' -> 'NCSINV2026117'."""
    if not s:
        return None
    toks = re.findall(r"[A-Za-z]+|\d+", s.upper())
    return "".join(str(int(t)) if t.isdigit() else t for t in toks)


class Stage:
    def __init__(self, key):
        self.key, self.checks, self.logs, self.data = key, [], [], {}

    def check(self, code, label, status, detail="", short=None):
        c = {"code": code, "label": label, "status": status, "detail": detail}
        if short:
            c["short"] = short
        self.checks.append(c)

    def log(self, msg):
        self.logs.append(msg)

    @property
    def status(self):
        if not self.checks:
            return "pass"
        worst = max(self.checks, key=lambda c: SEVERITY[c["status"]])["status"]
        return "pass" if worst == "info" else worst

    def to_dict(self, ms):
        return {"key": self.key, "status": self.status, "checks": self.checks, "logs": self.logs,
                "data": self.data, "ms": ms}


class Pipeline:
    def __init__(self, run_id: int, data: bytes, filename: str, emit):
        self.run_id, self.bytes, self.filename, self.emit = run_id, data, filename, emit
        self.r = store.RULES
        self.result = {"run_id": run_id, "filename": filename, "stages": [], "extracted": None, "vendor": None,
                       "po": None, "engine": None}
        self.mime = None
        self.text = ""
        self.text_source = None

    # ------------------------------------------------------------ driver
    def run(self) -> dict:
        t0 = time.time()
        for key, name, _ in STAGES:
            self.emit({"type": "stage_start", "stage": key})
            s = Stage(key)
            ts = time.time()
            try:
                getattr(self, f"stage_{key}")(s)
            except Exception as e:  # a crash in one stage must never produce a silent approval
                s.check("STAGE_ERROR", f"{name} failed", "hold", f"{type(e).__name__}: {e}")
            ms = int((time.time() - ts) * 1000)
            if PACING:
                time.sleep(max(0, PACING - (time.time() - ts)))
            sd = s.to_dict(ms)
            self.result["stages"].append(sd)
            self.emit({"type": "stage_done", "stage": key, "payload": sd})
            if key == "intake" and s.status == "hold" and self.mime is None:
                # unreadable file: skip to decision
                for k2, _, _ in STAGES[1:-1]:
                    skipped = Stage(k2)
                    skipped.check("SKIPPED", "Skipped - file could not be read", "info")
                    self.result["stages"].append(skipped.to_dict(0))
                    self.emit({"type": "stage_done", "stage": k2, "payload": skipped.to_dict(0)})
                d = Stage("decision")
                self.stage_decision(d)
                self.result["stages"].append(d.to_dict(0))
                self.emit({"type": "stage_done", "stage": "decision", "payload": d.to_dict(0)})
                break
        self.result["duration_ms"] = int((time.time() - t0) * 1000)
        return self.result

    def all_checks(self):
        return [c for st in self.result["stages"] for c in st["checks"]]

    # ------------------------------------------------------------ 1 intake
    def stage_intake(self, s: Stage):
        b = self.bytes
        s.data["sha256"] = hashlib.sha256(b).hexdigest()
        s.data["size_kb"] = round(len(b) / 1024, 1)
        if b[:4] == b"%PDF":
            self.mime = "application/pdf"
        elif b[:8] == b"\x89PNG\r\n\x1a\n":
            self.mime = "image/png"
        elif b[:3] == b"\xff\xd8\xff":
            self.mime = "image/jpeg"
        if not self.mime:
            s.check("UNREADABLE", "File type", "hold", "Not a PDF/PNG/JPG - cannot process. Ask vendor to resend.", "File could not be read")
            return
        s.data["type"] = self.mime
        s.log(f"Received {self.filename} ({s.data['size_kb']} KB, {self.mime})")
        s.log(f"SHA-256 fingerprint {s.data['sha256'][:16]}…")
        if self.mime == "application/pdf":
            try:
                with pdfplumber.open(io.BytesIO(b)) as pdf:
                    s.data["pages"] = len(pdf.pages)
                    self.text = "\n".join((p.extract_text() or "") for p in pdf.pages)
            except Exception as e:
                self.mime = None
                s.check("UNREADABLE", "PDF readable", "hold", f"Corrupt or encrypted PDF ({e}). Ask vendor to resend.", "File could not be read")
                return
            chars = len(re.sub(r"\s", "", self.text))
            s.data["text_chars"] = chars
            if chars >= 40:
                self.text_source = "text"
                s.check("TEXT_LAYER", "Machine-readable text", "pass", f"{chars} characters on {s.data['pages']} page(s)")
            else:
                s.check("TEXT_LAYER", "Machine-readable text", "info", "No text layer - scanned image, OCR required")
        else:
            s.data["pages"] = 1
            s.check("TEXT_LAYER", "Machine-readable text", "info", "Image upload - OCR required")

    def _ocr(self) -> str:
        if self.mime == "application/pdf":
            images = convert_from_bytes(self.bytes, dpi=300)
        else:
            images = [Image.open(io.BytesIO(self.bytes))]
        return "\n".join(pytesseract.image_to_string(im.convert("L"), config="--psm 6") for im in images)

    # ------------------------------------------------------------ 2 extraction
    def stage_extract(self, s: Stage):
        if self.text_source != "text":
            t = time.time()
            self.text = self._ocr()
            self.text_source = "ocr"
            s.log(f"OCR (Tesseract, 300 dpi) read {len(self.text)} characters in {time.time() - t:.1f}s")
        rules_out = extract_rules.extract(self.text, self.text_source)

        primary, engine = rules_out, f"rules ({'OCR' if self.text_source == 'ocr' else 'text layer'})"
        if extract_llm.provider():
            try:
                t = time.time()
                llm_out, llm_engine = extract_llm.extract(self.bytes, self.mime)
                s.log(f"LLM extraction via {llm_engine} in {time.time() - t:.1f}s")
                primary, engine = llm_out, llm_engine
                self._cross_check(s, llm_out, rules_out)
            except Exception as e:
                s.check("LLM_FALLBACK", "LLM extraction", "warn",
                        f"LLM unavailable ({str(e)[:200] or type(e).__name__}); fell back to deterministic rules engine")
        else:
            s.log("No LLM key configured - using deterministic rules engine")

        self.result["extracted"] = primary
        self.result["engine"] = engine
        s.data["engine"] = engine
        s.data["fields"] = primary
        found = [k for k in ["vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_number", "subtotal",
                             "total", "bank_account"] if primary.get(k) not in (None, "")]
        s.log(f"Extracted {len(found)}/8 header fields and {len(primary['line_items'])} line item(s)")
        conf = primary.get("field_confidence", {})
        key_conf = [conf.get(k, 0) for k in ["vendor_gstin", "invoice_number", "invoice_date", "total"]
                    if primary.get(k) not in (None, "")]
        overall = round(sum(key_conf) / len(key_conf), 2) if key_conf else 0.0
        s.data["confidence"] = overall
        if overall < self.r["min_extraction_confidence"]:
            s.check("LOW_CONFIDENCE", "Extraction confidence", "hold", f"{overall:.0%} - below threshold, needs human eyes", "Extraction uncertain - needs a human look")
        else:
            s.check("CONFIDENCE", "Extraction confidence", "pass", f"{overall:.0%} on key fields via {engine}")

    def _cross_check(self, s: Stage, a: dict, b: dict):
        """Two independent readers (LLM vs OCR/rules). Disagreement on money or identity -> human review."""
        diffs = []
        if a.get("total") is not None and b.get("total") is not None and abs(a["total"] - b["total"]) > 1:
            diffs.append(f"total {inr(a['total'])} vs {inr(b['total'])}")
        if a.get("invoice_number") and b.get("invoice_number") and canon_invoice_no(a["invoice_number"]) != canon_invoice_no(b["invoice_number"]):
            diffs.append(f"invoice no. {a['invoice_number']} vs {b['invoice_number']}")
        if a.get("bank_account") and b.get("bank_account") and a["bank_account"] != b["bank_account"]:
            diffs.append(f"bank a/c {a['bank_account']} vs {b['bank_account']}")
        if a.get("po_number") and b.get("po_number") and a["po_number"] != b["po_number"]:
            diffs.append(f"PO {a['po_number']} vs {b['po_number']}")
        if diffs:
            s.check("ENGINE_DISAGREE", "LLM vs OCR cross-check", "hold", "Engines disagree: " + "; ".join(diffs), "LLM and OCR read different values - needs a human look")
        else:
            s.check("ENGINE_AGREE", "LLM vs OCR cross-check", "pass", "Both engines agree on total, invoice no., PO, bank a/c")

    # ------------------------------------------------------------ 3 data quality
    def stage_quality(self, s: Stage):
        x = self.result["extracted"]
        missing = [lbl for k, lbl in [("invoice_number", "invoice number"), ("invoice_date", "invoice date"),
                                      ("total", "invoice total")] if x.get(k) in (None, "")]
        if not x.get("vendor_gstin") and not x.get("vendor_name"):
            missing.append("vendor identity")
        if missing:
            s.check("MISSING_FIELD", "Mandatory fields present", "hold", "Missing: " + ", ".join(missing), "Missing " + ", ".join(missing))
        else:
            s.check("MANDATORY_OK", "Mandatory fields present", "pass", "Invoice no., date, total and vendor identity found")

        tol = self.r["rounding_tolerance_abs"]
        lines = x.get("line_items") or []
        if lines and x.get("subtotal") is not None:
            ls = round(sum(li["amount"] or 0 for li in lines), 2)
            if abs(ls - x["subtotal"]) > tol:
                s.check("ARITHMETIC", "Line items add up to subtotal", "hold", f"Lines sum to {inr(ls)}, subtotal says {inr(x['subtotal'])}", "Invoice arithmetic doesn't add up")
            else:
                s.check("LINES_SUM_OK", "Line items add up to subtotal", "pass", inr(ls))
        elif not lines:
            s.check("NO_LINES", "Line items readable", "warn", "No itemised lines found - only header-level match possible")
        if x.get("subtotal") is not None and x.get("total") is not None:
            exp = round(x["subtotal"] + (x.get("tax_total") or 0), 2)
            if abs(exp - x["total"]) > tol:
                s.check("ARITHMETIC", "Subtotal + tax = total", "hold", f"{inr(x['subtotal'])} + {inr(x.get('tax_total') or 0)} = {inr(exp)}, invoice says {inr(x['total'])}", "Invoice arithmetic doesn't add up")
            else:
                s.check("TOTAL_OK", "Subtotal + tax = total", "pass", f"{inr(x['subtotal'])} + {inr(x.get('tax_total') or 0)} = {inr(x['total'])}")

        if x.get("invoice_date"):
            try:
                d = date.fromisoformat(x["invoice_date"])
                age = (date.today() - d).days
                if age < 0:
                    s.check("FUTURE_DATE", "Invoice date sane", "hold", f"Dated {d:%d %b %Y} - in the future", "Invoice is dated in the future")
                elif age > self.r["max_invoice_age_days"]:
                    s.check("STALE", "Invoice date sane", "hold", f"{age} days old - beyond {self.r['max_invoice_age_days']}-day policy", "Invoice is too old to pay under policy")
                else:
                    s.check("DATE_OK", "Invoice date sane", "pass", f"{d:%d %b %Y} ({age} days ago)")
            except ValueError:
                s.check("BAD_DATE", "Invoice date sane", "hold", f"Unparseable date '{x['invoice_date']}'")

        if x.get("vendor_gstin"):
            ok, why = gstin.validate(x["vendor_gstin"])
            s.check("GSTIN_VALID" if ok else "GSTIN_INVALID", "Vendor GSTIN valid (format + checksum)",
                    "pass" if ok else "hold", f"{x['vendor_gstin']} - {why}", None if ok else "Vendor GSTIN is invalid")

        company = store.COMPANY
        if x.get("buyer_gstin"):
            if x["buyer_gstin"] == company["gstin"]:
                s.check("BILLED_TO_US", "Addressed to our entity", "pass", f"Buyer GSTIN {company['gstin']} ({company['name']})")
            else:
                s.check("NOT_OUR_INVOICE", "Addressed to our entity", "reject",
                        f"Buyer GSTIN {x['buyer_gstin']} is not ours ({company['gstin']})", "Invoice is addressed to another company")
        else:
            s.check("NO_BUYER_GSTIN", "Addressed to our entity", "warn", "Our GSTIN not printed - input tax credit at risk")

    # ------------------------------------------------------------ 4 vendor
    def stage_vendor(self, s: Stage):
        x = self.result["extracted"]
        vendor, how = None, None
        if x.get("vendor_gstin"):
            vendor = next((v for v in store.VENDORS if v["gstin"] == x["vendor_gstin"]), None)
            how = "GSTIN"
        if not vendor and x.get("vendor_name"):
            best = max(store.VENDORS, key=lambda v: fuzz.token_set_ratio(v["name"].lower(), x["vendor_name"].lower()))
            score = fuzz.token_set_ratio(best["name"].lower(), x["vendor_name"].lower())
            if score >= 90:
                vendor, how = best, f"name ({score:.0f}% match)"
        if not vendor:
            s.check("VENDOR_UNKNOWN", "Vendor in approved master", "reject",
                    f"'{x.get('vendor_name')}' / {x.get('vendor_gstin') or 'no GSTIN'} is not an onboarded vendor - route to Procurement for onboarding", "Vendor is not in the approved vendor master")
            return
        self.result["vendor"] = vendor
        s.data["vendor"] = vendor
        s.check("VENDOR_FOUND", "Vendor in approved master", "pass", f"{vendor['vendor_id']} {vendor['name']} (matched on {how})")
        if x.get("vendor_gstin") and x["vendor_gstin"] != vendor["gstin"]:
            s.check("GSTIN_MISMATCH", "GSTIN matches vendor master", "hold",
                    f"Invoice {x['vendor_gstin']} vs master {vendor['gstin']}", "GSTIN differs from vendor master")
        if vendor["status"] != "approved":
            s.check("VENDOR_BLOCKED", "Vendor status active", "reject",
                    f"Status '{vendor['status']}': {vendor.get('status_reason', '')}", f"Vendor is {vendor['status']} - {vendor.get('status_reason', '')}")
        else:
            s.check("VENDOR_ACTIVE", "Vendor status active", "pass", "Approved")

        mb = vendor["bank"]
        if x.get("bank_account"):
            if x["bank_account"] != mb["account_number"] or (x.get("ifsc") and x["ifsc"] != mb["ifsc"]):
                note = " Invoice text: “" + x["notes"][0] + "”" if x.get("notes") else ""
                s.check("BANK_MISMATCH", "Remit-to bank matches master", "hold",
                        f"Invoice asks for payment to {x.get('bank_name') or ''} a/c {x['bank_account']} (IFSC {x.get('ifsc') or '?'}); "
                        f"master has {mb['bank_name']} a/c {mb['account_number']} (IFSC {mb['ifsc']}).{note}",
                        "Remit-to bank account differs from vendor master - possible payment fraud")
            else:
                s.check("BANK_OK", "Remit-to bank matches master", "pass", f"{mb['bank_name']} a/c …{mb['account_number'][-4:]}")
        else:
            s.check("BANK_ABSENT", "Remit-to bank matches master", "info", "No bank details on invoice - pay to master account")

        types = {t["type"] for t in x.get("tax_lines") or []}
        if types:
            intra = vendor["gstin"][:2] == store.COMPANY["state_code"]
            expected = {"CGST", "SGST"} if intra else {"IGST"}
            if types == expected or (intra and types == {"CGST", "UTGST"}):
                s.check("TAX_TYPE_OK", "GST type fits place of supply", "pass",
                        f"{'Intra' if intra else 'Inter'}-state supply → {' + '.join(sorted(types))}")
            else:
                s.check("TAX_TYPE_WRONG", "GST type fits place of supply", "hold",
                        f"{'Intra' if intra else 'Inter'}-state supply should be {' + '.join(sorted(expected))}, invoice charges {' + '.join(sorted(types))}", "Wrong GST type for place of supply")

    # ------------------------------------------------------------ 5 duplicates
    def stage_duplicate(self, s: Stage):
        x, v = self.result["extracted"], self.result["vendor"]
        sha = self.result["stages"][0]["data"].get("sha256")
        num = canon_invoice_no(x.get("invoice_number"))
        vid = v["vendor_id"] if v else None
        hits = 0
        for e in store.ledger():
            when = (f"paid {date.fromisoformat(e['paid_on']):%d %b %Y}" if e.get("paid_on")
                    else f"approved in {e.get('source', 'ledger')}, awaiting payment")
            label = f"{e['invoice_number']} ({when})"
            if sha and e.get("file_sha256") == sha:
                s.check("DUP_FILE", "Exact file seen before", "reject", f"Identical file already booked as {label}", f"Duplicate - this exact file was already booked as {label}")
                hits += 1
            elif vid and e["vendor_id"] == vid and num and canon_invoice_no(e["invoice_number"]) == num:
                same_fmt = e["invoice_number"] == x["invoice_number"]
                s.check("DUP_NUMBER", "Invoice number unique for vendor", "reject",
                        f"'{x['invoice_number']}' {'is' if same_fmt else 'is the same number as'} {label}"
                        + ("" if same_fmt else " - only the formatting differs"), f"Duplicate of {label}")
                hits += 1
            elif vid and e["vendor_id"] == vid and x.get("total") is not None and abs(e["total"] - x["total"]) <= 1 and x.get("invoice_date"):
                gap = abs((date.fromisoformat(e["invoice_date"]) - date.fromisoformat(x["invoice_date"])).days)
                if gap <= self.r["suspected_duplicate_window_days"]:
                    s.check("DUP_SUSPECT", "No same-amount invoice nearby", "hold",
                            f"Same vendor, same amount {inr(x['total'])}, {gap} days apart from {label} - different number", f"Possible duplicate of {e['invoice_number']} (same amount, {gap} days apart)")
                    hits += 1
        for pr in store.prior_runs(self.run_id):
            if pr["decision"] != "ON HOLD":
                continue
            pnum = canon_invoice_no(pr["extracted"].get("invoice_number"))
            if (sha and pr["sha256"] == sha) or (vid and pr["vendor_id"] == vid and num and pnum == num):
                s.check("DUP_IN_FLIGHT", "Not already in exception queue", "hold",
                        f"Same invoice is already on hold as run #{pr['run_id']} - resolve that one first", f"Already in the exception queue as run #{pr['run_id']}")
                hits += 1
        if not hits:
            s.check("NO_DUP", "Duplicate check", "pass",
                    f"Checked {len(store.ledger())} booked invoices + open exceptions: no match on file, number or amount/date")
        s.data["canonical_number"] = num

    # ------------------------------------------------------------ 6 PO matching
    def _line_match(self, inv_lines, po):
        used = store.po_consumption(po["po_number"])
        matches = []
        for li in inv_lines:
            best, score = None, 0
            for pl in po["lines"]:
                sc = fuzz.token_set_ratio(li["description"].lower(), pl["description"].lower())
                if sc > score:
                    best, score = pl, sc
            if best is None or score < self.r["description_match_min"]:
                matches.append({"invoice_line": li, "po_line": None, "score": score})
                continue
            u = used.get(best["line"], {"quantity": 0, "amount": 0, "invoices": []})
            matches.append({"invoice_line": li, "po_line": best, "score": score,
                            "billed_before_qty": u["quantity"], "billed_before_invoices": u["invoices"],
                            "remaining_qty": best["quantity"] - u["quantity"]})
        return matches

    def _po_remaining_value(self, po):
        used = store.po_consumption(po["po_number"])
        return round(sum(pl["quantity"] * pl["unit_price"] for pl in po["lines"])
                     - sum(u["amount"] for u in used.values()), 2)

    def stage_po_match(self, s: Stage):
        x, v = self.result["extracted"], self.result["vendor"]
        po = None
        if x.get("po_number"):
            po = next((p for p in store.POS if p["po_number"] == x["po_number"]), None)
            if not po:
                s.check("PO_NOT_FOUND", "PO exists", "hold", f"{x['po_number']} not found in procurement system", f"{x['po_number']} does not exist")
                return
            s.check("PO_FOUND", "PO exists", "pass", f"{po['po_number']} quoted on invoice")
            if v and po["vendor_id"] != v["vendor_id"]:
                s.check("PO_VENDOR_MISMATCH", "PO belongs to this vendor", "hold",
                        f"{po['po_number']} was raised on {po['vendor_id']}, invoice is from {v['vendor_id']}", "PO belongs to a different vendor")
        else:
            if not v:
                s.check("PO_UNRESOLVED", "PO identified", "hold", "No PO on invoice and vendor unknown", "Could not identify the PO")
                return
            cands = [p for p in store.POS if p["vendor_id"] == v["vendor_id"] and p["status"] == "open"]
            s.log(f"No PO number on invoice - scoring {len(cands)} open PO(s) for {v['name']}")
            scored = []
            for p in cands:
                m = self._line_match(x.get("line_items") or [], p)
                line_score = (sum(1 for mm in m if mm["po_line"]) / len(m)) if m else 0
                rem = self._po_remaining_value(p)
                amt_score = max(0.0, 1 - abs((x.get("subtotal") or 0) - rem) / rem) if rem > 0 else 0
                score = round(0.6 * line_score + 0.4 * amt_score, 2)
                scored.append((score, p))
                s.log(f"  {p['po_number']}: lines {line_score:.0%}, amount fit {amt_score:.0%} → score {score:.2f}")
            scored.sort(key=lambda t: -t[0])
            s.data["candidates"] = [{"po_number": p["po_number"], "score": sc} for sc, p in scored]
            if scored and scored[0][0] >= self.r["po_inference_min_score"] and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.2):
                po = scored[0][1]
                s.check("PO_INFERRED", "PO identified", "warn",
                        f"Not quoted on invoice; inferred {po['po_number']} (score {scored[0][0]:.2f}, next best "
                        f"{scored[1][0]:.2f})" if len(scored) > 1 else f"inferred {po['po_number']} (only open PO)")
            else:
                s.check("PO_UNRESOLVED", "PO identified", "hold", "No PO quoted and no confident match among open POs", "Could not identify the PO")
                return

        self.result["po"] = po
        s.data["po"] = po
        if po["status"] != "open":
            s.check("PO_CLOSED", "PO open", "hold", f"{po['po_number']} is {po['status']}", f"{po['po_number']} is {po['status']}")

        tol_pct = self.r["price_tolerance_pct"]
        matches = self._line_match(x.get("line_items") or [], po)
        s.data["line_matches"] = matches
        overrun_value, ok_value = 0.0, 0.0
        for m in matches:
            li, pl = m["invoice_line"], m["po_line"]
            if not pl:
                s.check("LINE_UNMATCHED", f"Line '{li['description']}'", "hold", "No matching item on the PO", "Invoice has an item that is not on the PO")
                continue
            var = (li["unit_price"] - pl["unit_price"]) / pl["unit_price"] * 100
            if var > tol_pct:
                s.check("PRICE_VARIANCE", f"Price: {pl['description']}", "hold",
                        f"Billed {inr(li['unit_price'])} vs PO {inr(pl['unit_price'])} (+{var:.1f}%, tolerance {tol_pct}%)", f"{pl['description']} billed {var:.1f}% above PO price")
            elif abs(var) > 0.01:
                s.check("PRICE_WITHIN_TOL", f"Price: {pl['description']}", "warn" if var > 0 else "pass",
                        f"Billed {inr(li['unit_price'])} vs PO {inr(pl['unit_price'])} ({var:+.1f}%, within tolerance)")
            else:
                s.check("PRICE_OK", f"Price: {pl['description']}", "pass", f"{inr(li['unit_price'])} = PO price")
            rem = m["remaining_qty"]
            prior = (f"; {m['billed_before_qty']:g} already billed on {', '.join(m['billed_before_invoices'])}"
                     if m["billed_before_qty"] else "")
            if li["quantity"] > rem + 1e-9:
                over = li["quantity"] - max(rem, 0)
                overrun_value += over * pl["unit_price"]
                ok_value += max(rem, 0) * min(li["unit_price"], pl["unit_price"])
                s.check("QTY_EXCEEDS_PO", f"Quantity: {pl['description']}", "hold",
                        f"Bills {li['quantity']:g}, PO {pl['quantity']:g}{prior} → only {max(rem, 0):g} left; "
                        f"over by {over:g} ({inr(over * pl['unit_price'])})",
                        f"Bills {over:g} more {pl['description']} than remain on {po['po_number']} ({inr(over * pl['unit_price'])} over)")
            else:
                ok_value += li["quantity"] * min(li["unit_price"], pl["unit_price"])
                s.check("QTY_OK", f"Quantity: {pl['description']}", "pass",
                        f"{li['quantity']:g} of {pl['quantity']:g} ordered{prior}")

        rem_val = self._po_remaining_value(po)
        sub = x.get("subtotal")
        if sub is not None:
            diff = sub - rem_val
            limit = min(rem_val * self.r["amount_tolerance_pct"] / 100, self.r["amount_tolerance_abs"]) if rem_val > 0 else 0
            if diff > limit:
                s.check("PO_BALANCE_EXCEEDED", "Within remaining PO value", "hold",
                        f"Invoice {inr(sub)} (pre-tax) vs {inr(rem_val)} left on {po['po_number']} → over by {inr(diff)}", f"Exceeds remaining {po['po_number']} value by {inr(diff)}")
            else:
                s.check("PO_BALANCE_OK", "Within remaining PO value", "pass",
                        f"{inr(sub)} of {inr(rem_val)} remaining on {po['po_number']}")
        s.data["po_remaining_before"] = rem_val
        s.data["releasable_value"] = round(ok_value, 2)
        s.data["overrun_value"] = round(overrun_value, 2)

        rates = {t["rate"] for t in x.get("tax_lines") or [] if t.get("rate") is not None}
        if rates and sub:
            total_rate = sum(t["rate"] for t in x["tax_lines"] if t.get("rate") is not None)
            exp_tax = round(sub * po["gst_rate"] / 100, 2)
            if abs(total_rate - po["gst_rate"]) > 0.01:
                s.check("TAX_RATE_MISMATCH", "GST rate matches PO", "hold", f"Invoice charges {total_rate:g}%, PO says {po['gst_rate']}%", "GST rate differs from PO")
            elif x.get("tax_total") is not None and abs(x["tax_total"] - exp_tax) > self.r["rounding_tolerance_abs"]:
                s.check("TAX_AMOUNT_WRONG", "GST amount correct", "hold", f"Tax {inr(x['tax_total'])}, expected {inr(exp_tax)}", "GST amount is wrong")
            else:
                s.check("TAX_OK", "GST rate & amount", "pass", f"{po['gst_rate']}% = {inr(exp_tax)}")

    # ------------------------------------------------------------ 7 decision
    ROUTES = {
        "BANK_MISMATCH": "Treasury / Vendor Master (fraud verification)",
        "GSTIN_MISMATCH": "Vendor Master team",
        "VENDOR_UNKNOWN": "Procurement (vendor onboarding)",
        "VENDOR_BLOCKED": "Procurement (vendor compliance)",
        "QTY_EXCEEDS_PO": "PO owner in Procurement",
        "PO_BALANCE_EXCEEDED": "PO owner in Procurement",
        "PRICE_VARIANCE": "PO owner in Procurement",
        "LINE_UNMATCHED": "PO owner in Procurement",
        "PO_NOT_FOUND": "PO owner in Procurement",
        "PO_UNRESOLVED": "PO owner in Procurement",
        "PO_VENDOR_MISMATCH": "PO owner in Procurement",
        "PO_CLOSED": "PO owner in Procurement",
    }
    VENDOR_FIXABLE = {"MISSING_FIELD", "ARITHMETIC", "BAD_DATE", "FUTURE_DATE", "GSTIN_INVALID", "TAX_TYPE_WRONG",
                      "TAX_RATE_MISMATCH", "TAX_AMOUNT_WRONG", "UNREADABLE", "NOT_OUR_INVOICE"}

    def stage_decision(self, s: Stage):
        checks = self.all_checks()
        rejects = [c for c in checks if c["status"] == "reject"]
        holds = [c for c in checks if c["status"] == "hold"]
        warns = [c for c in checks if c["status"] == "warn"]
        x = self.result.get("extracted") or {}
        v = self.result.get("vendor")
        po = self.result.get("po")
        pm = next((st["data"] for st in self.result["stages"] if st["key"] == "po_match"), {})

        if rejects:
            outcome, blocking = "REJECTED", rejects
        elif holds:
            outcome, blocking = "ON HOLD", holds
        else:
            outcome, blocking = "APPROVED", []

        priority = ["DUP_FILE", "DUP_NUMBER", "VENDOR_BLOCKED", "VENDOR_UNKNOWN", "NOT_OUR_INVOICE", "BANK_MISMATCH",
                    "GSTIN_MISMATCH", "DUP_IN_FLIGHT", "DUP_SUSPECT", "QTY_EXCEEDS_PO", "PO_BALANCE_EXCEEDED",
                    "PRICE_VARIANCE", "MISSING_FIELD", "ARITHMETIC"]
        blocking = sorted(blocking, key=lambda c: priority.index(c["code"]) if c["code"] in priority else 99)
        codes = [c["code"] for c in blocking]

        if outcome == "APPROVED":
            headline = f"Approved for payment: {inr(x.get('total'))} to {v['name']}" if v else "Approved"
            routing = "Payment run (auto)"
            payable = x.get("total")
            nxt = f"Schedule payment of {inr(payable)} to {v['bank']['bank_name']} a/c …{v['bank']['account_number'][-4:]} " \
                  f"(account from vendor master) and book against {po['po_number']}."
        else:
            top = blocking[0]
            headline = top.get("short") or f"{top['label']}: {top['detail']}"
            routing = self.ROUTES.get(top["code"], "AP Exceptions queue")
            payable = None
            nxt = self._next_action(top["code"], x, v, po, pm)
            if outcome == "ON HOLD" and "QTY_EXCEEDS_PO" in codes and pm.get("releasable_value") and \
                    not any(c not in ("QTY_EXCEEDS_PO", "PO_BALANCE_EXCEEDED") for c in codes):
                gst = po["gst_rate"] / 100
                rel = round(pm["releasable_value"] * (1 + gst), 2)
                nxt += f" Option: release {inr(rel)} now (the in-PO portion incl. GST) and hold {inr(round(pm['overrun_value'] * (1 + gst), 2))}."

        s.data.update({
            "outcome": outcome, "headline": headline, "reasons": blocking, "warnings": warns,
            "routing": routing, "payable": payable, "next_action": nxt,
            "vendor_message": self._vendor_message(outcome, codes, blocking, x, v),
            "summary": self._summary(outcome, x, v, po, blocking, warns),
        })
        s.check("DECISION", "Decision", {"APPROVED": "pass", "ON HOLD": "hold", "REJECTED": "reject"}[outcome], headline)
        self.result["decision"] = s.data
        if outcome == "APPROVED" and v and po:
            store.add_to_ledger({
                "vendor_id": v["vendor_id"], "invoice_number": x["invoice_number"], "invoice_date": x["invoice_date"],
                "po_number": po["po_number"], "subtotal": x.get("subtotal"), "tax_total": x.get("tax_total"),
                "total": x["total"], "status": "approved", "paid_on": None,
                "lines": [{"po_line": m["po_line"]["line"], "quantity": m["invoice_line"]["quantity"],
                           "amount": m["invoice_line"]["amount"]} for m in pm.get("line_matches", []) if m["po_line"]],
                "file_sha256": self.result["stages"][0]["data"].get("sha256"), "source": f"run #{self.run_id}",
            })
            s.log("Booked to ledger - PO balance and duplicate index updated")

    def _next_action(self, code, x, v, po, pm):
        if code == "BANK_MISMATCH":
            return ("Do NOT update bank details from the invoice. Call the vendor on the phone number held in the vendor "
                    "master (not one on the invoice/email), confirm the change, and require a cancelled cheque / bank letter "
                    "before Vendor Master updates the account. Pay only after verification.")
        if code in ("DUP_NUMBER", "DUP_FILE"):
            return "Do not pay. Inform vendor it was already processed; if they believe it's a new bill, ask for a distinct invoice."
        if code == "DUP_SUSPECT":
            return "Compare both invoices side by side; confirm with vendor whether this is a separate supply."
        if code == "DUP_IN_FLIGHT":
            return "Resolve the earlier exception first; close this one as a duplicate submission."
        if code in ("QTY_EXCEEDS_PO", "PO_BALANCE_EXCEEDED"):
            return (f"PO owner ({po['buyer']}) to confirm whether the extra quantity was ordered and received; if yes, amend "
                    f"{po['po_number']}, otherwise ask vendor for a revised invoice / credit note.")
        if code == "PRICE_VARIANCE":
            return f"PO owner ({po['buyer'] if po else 'Procurement'}) to approve the price change or ask vendor for a corrected invoice."
        if code == "VENDOR_BLOCKED":
            return "Do not pay. Procurement to decide whether to re-verify the vendor; AP keeps invoice parked."
        if code == "VENDOR_UNKNOWN":
            return "Procurement to onboard the vendor (KYC, bank, GSTIN) before this invoice can be processed."
        if code in self.VENDOR_FIXABLE:
            return "Send the vendor the drafted request below; re-run when the corrected invoice arrives."
        return "AP analyst to review the flagged checks."

    def _vendor_message(self, outcome, codes, blocking, x, v):
        if outcome == "APPROVED" or not v:
            return None
        to = v["contact_email"]
        ref = x.get("invoice_number") or (f"dated {date.fromisoformat(x['invoice_date']):%d %b %Y}" if x.get("invoice_date") else "(no number, no date)")
        if "BANK_MISMATCH" in codes:
            return {"to": "(none - verify by phone using vendor-master contact)", "subject": None,
                    "body": "No email is sent for bank changes: replying to an email could reach a fraudster. Verification is by call-back."}
        if any(c in ("DUP_NUMBER", "DUP_FILE") for c in codes):
            dup = next(c for c in blocking if c["code"] in ("DUP_NUMBER", "DUP_FILE"))
            return {"to": to, "subject": f"Invoice {ref} - already processed",
                    "body": f"Hello {v['name']} team,\n\nWe received invoice {ref} for {inr(x.get('total'))}. Our records show this bill "
                            f"was already processed: {dup['detail']}.\n\nWe are therefore not processing it again. If this is a separate "
                            f"supply, please send a new invoice with a distinct invoice number and the relevant PO reference.\n\n"
                            f"Regards,\nAccounts Payable, {store.COMPANY['name']}\n{store.COMPANY['ap_email']}"}
        fixable = [c for c in blocking if c["code"] in self.VENDOR_FIXABLE]
        if fixable:
            items = "\n".join(f"  - {c['label']}: {c['detail']}" for c in fixable)
            return {"to": to, "subject": f"Action needed: invoice {ref} cannot be processed yet",
                    "body": f"Hello {v['name']} team,\n\nWe received your invoice {ref} for {inr(x.get('total'))} but cannot process it "
                            f"until the following is corrected:\n{items}\n\nPlease send a corrected invoice to {store.COMPANY['ap_email']}. "
                            f"Payment terms will run from the date we receive the corrected copy.\n\nRegards,\nAccounts Payable, "
                            f"{store.COMPANY['name']}"}
        if any(c in ("QTY_EXCEEDS_PO", "PO_BALANCE_EXCEEDED", "PRICE_VARIANCE") for c in codes):
            return {"to": "(internal first)", "subject": None,
                    "body": "Vendor is contacted only if the PO owner confirms the excess was not ordered - then request a revised invoice or credit note."}
        return None

    def _summary(self, outcome, x, v, po, blocking, warns):
        who = v["name"] if v else (x.get("vendor_name") or "Unknown vendor")
        head = f"Invoice {x.get('invoice_number') or '(no number)'} from {who} for {inr(x.get('total'))}"
        if po:
            head += f" against {po['po_number']}"
        if outcome == "APPROVED":
            tail = " passed every check and is approved for payment."
            if warns:
                tail += " Noted: " + "; ".join(w["detail"] for w in warns) + "."
            return head + tail
        verb = "was rejected" if outcome == "REJECTED" else "is on hold"
        why = "; ".join((c.get("short") or c["label"]).rstrip(".") for c in blocking[:3])
        extra = f" ({len(blocking) - 3} more issue(s) listed below)" if len(blocking) > 3 else ""
        return head + f" {verb}: {why[0].lower() + why[1:]}{extra}."
