"""LLM path tests without a real key: (1) bad key -> graceful fallback, (2) mocked Claude reply -> normalise + cross-check."""
import hashlib, json, os, sys, tempfile
os.environ["DEMO_PACING_MS"] = "0"
os.environ["RUNTIME_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import store, extract_llm
from app.pipeline import Pipeline
store.init()

def run(fname):
    data = (store.SAMPLES / fname).read_bytes()
    rid = store.create_run(fname, hashlib.sha256(data).hexdigest())
    res = Pipeline(rid, data, fname, lambda e: None).run(); store.finish_run(rid, res); return res

# 1) invalid key: real HTTP call fails -> must fall back to rules, still decide correctly
os.environ["ANTHROPIC_API_KEY"] = "sk-invalid"
res = run("05_edge_bank_details_changed_shree_ganesh.pdf")
ex = [c for c in res["stages"][1]["checks"]]
print("fallback:", res["engine"], "|", [ (c["code"], c["status"]) for c in ex], "|", res["decision"]["outcome"])
assert res["engine"].startswith("rules") and any(c["code"] == "LLM_FALLBACK" for c in ex) and res["decision"]["outcome"] == "ON HOLD"

# 2) mocked Claude success (numbers as strings w/ commas, PO lowercase) -> normalised, engines agree
class R:
    status_code = 200
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return {"content": [{"type": "text", "text": "```json\n" + json.dumps(self.payload) + "\n```"}]}
reply = {"vendor_name": "Shree Ganesh Packaging Pvt Ltd", "vendor_gstin": "27AAKCS3374M1ZQ", "buyer_name": "Meridian Foods Pvt Ltd",
         "buyer_gstin": "29AAJCM4821K1Z3", "invoice_number": "SGP/26-27/1187", "invoice_date": "2026-11-14", "po_number": "po 4501",
         "currency": "INR", "line_items": [{"description": "Corrugated boxes 5-ply 18x12x12 in", "quantity": "2,000", "unit_price": "42.50", "amount": "85,000.00"},
                                          {"description": "Printed carton labels", "quantity": 5000, "unit_price": 3.2, "amount": 16000}],
         "subtotal": "1,01,000.00", "tax_lines": [{"type": "igst", "rate": 18, "amount": "18,180"}], "tax_total": None, "total": "₹1,19,180.00",
         "bank_account": "5020 0012 3456 78", "ifsc": "hdfc0000123", "bank_name": "HDFC Bank", "notes": [],
         "field_confidence": {"vendor_gstin": 0.98, "invoice_number": 0.97, "invoice_date": 0.95, "total": 0.99}}
extract_llm.httpx.post = lambda *a, **k: R(reply)
res = run("01_happy_path_shree_ganesh.pdf")
c = {x["code"]: x for x in res["stages"][1]["checks"]}
print("mocked:", res["engine"], "|", list(c), "|", res["decision"]["outcome"], "|", res["extracted"]["po_number"], res["extracted"]["total"])
assert res["engine"].startswith("claude:") and "ENGINE_AGREE" in c and res["decision"]["outcome"] == "APPROVED"

# 3) mocked LLM misreads the total -> cross-check must force a HOLD
reply2 = dict(reply, invoice_number="SGP/26-27/1203", po_number="PO-4505", total="2,51,940.00",
              line_items=[{"description": "Stretch wrap film 23 micron (roll)", "quantity": 300, "unit_price": 610, "amount": 183000}],
              subtotal=183000, tax_lines=[{"type": "IGST", "rate": 18, "amount": 32940}])
extract_llm.httpx.post = lambda *a, **k: R(reply2)
store.reset()
res = run("05_edge_bank_details_changed_shree_ganesh.pdf")
codes = [x["code"] for st in res["stages"] for x in st["checks"] if x["status"] in ("hold", "reject")]
print("disagree:", codes, "|", res["decision"]["outcome"])
assert "ENGINE_DISAGREE" in codes and res["decision"]["outcome"] == "ON HOLD"

# 4) Gemini: configured model 404 -> auto-discover newest stable flash model -> succeed
del os.environ["ANTHROPIC_API_KEY"]; os.environ["GEMINI_API_KEY"] = "test-key"
class G:
    def __init__(self, code, payload): self.status_code, self.payload, self.text = code, payload, json.dumps(payload)
    def json(self): return self.payload
calls = []
models = {"models": [
    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.6-flash-lite", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.7-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.9-flash-preview", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.7-flash-image", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}]}
def gpost(url, **k):
    calls.append(url)
    if "gemini-3.7-flash:" in url:
        return G(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]})
    return G(404, {"error": {"message": "models/x is not found for API version v1beta"}})
extract_llm.httpx.post = gpost
extract_llm.httpx.get = lambda url, **k: G(200, models)
store.reset()
res = run("01_happy_path_shree_ganesh.pdf")
print("gemini auto:", res["engine"], [c["code"] for c in res["stages"][1]["checks"]], res["decision"]["outcome"])
assert res["engine"] == "gemini:gemini-3.7-flash" and res["decision"]["outcome"] == "APPROVED"

# 5) invalid key -> readable status + message in the fallback note, still decides
extract_llm._GEMINI_AUTO = None
extract_llm.httpx.post = lambda url, **k: G(400, {"error": {"message": "API key not valid. Please pass a valid API key."}})
store.reset()
res = run("01_happy_path_shree_ganesh.pdf")
note = res["stages"][1]["checks"][0]["detail"]
print("bad key note:", note)
assert "HTTP 400" in note and "API key not valid" in note and res["decision"]["outcome"] == "APPROVED"

# 6) overload: preferred model 503 (twice) -> fail over to next available Flash model -> succeed
extract_llm.time.sleep = lambda s: None
extract_llm._GEMINI_AUTO = None
seen = []
def overload(url, **k):
    seen.append(url.split("/models/")[1].split(":")[0])
    if "gemini-3.6-flash:" in url or "gemini-3.7-flash:" in url:
        return G(503, {"error": {"message": "This model is currently experiencing high demand."}})
    if "gemini-2.5-flash:" in url:
        return G(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]})
    return G(404, {"error": {"message": "not found"}})
extract_llm.httpx.post = overload
store.reset()
res = run("01_happy_path_shree_ganesh.pdf")
print("failover:", seen, "->", res["engine"], res["decision"]["outcome"])
assert res["engine"] == "gemini:gemini-2.5-flash" and seen.count("gemini-3.6-flash") == 2 and res["decision"]["outcome"] == "APPROVED"

# 7) everything overloaded -> clean fallback to rules with the 503 message, decision unaffected
extract_llm._GEMINI_AUTO = None
extract_llm.httpx.post = lambda url, **k: G(503, {"error": {"message": "This model is currently experiencing high demand."}})
store.reset()
res = run("03_edge_split_billing_overrun_apex.pdf")
note = res["stages"][1]["checks"][0]["detail"]
print("all busy:", note[:110], "|", res["decision"]["outcome"])
assert "503" in note and res["engine"].startswith("rules") and res["decision"]["outcome"] == "ON HOLD"

# 8) Mistral: OCR -> chat JSON; first chat call rate-limited (429) then succeeds; preferred over Gemini when both keys set
os.environ["MISTRAL_API_KEY"] = "test-mistral"
os.environ["MISTRAL_USE_OCR"] = "1"
mcalls = []
def mpost(url, **k):
    mcalls.append(url.rsplit("/v1/", 1)[1])
    if url.endswith("/v1/ocr"):
        assert k["json"]["document"]["document_url"].startswith("data:application/pdf;base64,")
        assert k["headers"]["Authorization"] == "Bearer test-mistral"
        return G(200, {"pages": [{"index": 0, "markdown": "# TAX INVOICE\nShree Ganesh Packaging ... Grand Total 1,19,180.00"}]})
    if mcalls.count("chat/completions") == 1:
        return G(429, {"message": "Requests rate limit exceeded"})
    assert k["json"]["response_format"] == {"type": "json_object"}
    return G(200, {"choices": [{"message": {"content": json.dumps(reply)}}]})
extract_llm.httpx.post = mpost
store.reset()
res = run("01_happy_path_shree_ganesh.pdf")
print("mistral:", mcalls, "->", res["engine"], [c["code"] for c in res["stages"][1]["checks"]], res["decision"]["outcome"])
assert res["engine"].startswith("mistral:mistral-ocr-latest") and "ENGINE_AGREE" in [c["code"] for c in res["stages"][1]["checks"]]
assert mcalls == ["ocr", "chat/completions", "chat/completions"] and res["decision"]["outcome"] == "APPROVED"

# 9) Mistral bad key -> readable 401 in the note, rules fallback, decision still right
extract_llm.httpx.post = lambda url, **k: G(401, {"message": "Unauthorized"})
store.reset()
res = run("04_edge_disguised_duplicate_nimbus.pdf")
note = res["stages"][1]["checks"][0]["detail"]
print("mistral bad key:", note[:100], "|", res["decision"]["outcome"])
assert "HTTP 401" in note and res["decision"]["outcome"] == "REJECTED"

# 10) default mode (no MISTRAL_USE_OCR): text PDF -> single chat call with our text, no OCR endpoint
del os.environ["MISTRAL_USE_OCR"]
mcalls.clear()
def mpost2(url, **k):
    mcalls.append(url.rsplit("/v1/", 1)[1])
    assert "SGP/26-27/1187" in k["json"]["messages"][0]["content"]
    return G(200, {"choices": [{"message": {"content": json.dumps(reply)}}]})
extract_llm.httpx.post = mpost2
store.reset()
res = run("01_happy_path_shree_ganesh.pdf")
print("mistral text mode:", mcalls, res["engine"], res["decision"]["outcome"])
assert mcalls == ["chat/completions"] and res["engine"] == "mistral:mistral-small-latest" and res["decision"]["outcome"] == "APPROVED"

# 11) scanned invoice in default mode -> our Tesseract text goes to chat (no OCR endpoint)
mcalls.clear()
scan_reply = {"vendor_name": "Kaveri Office Solutions", "vendor_gstin": "29AAGFK9043D1Z9", "buyer_gstin": "29AAJCM4821K1Z3",
  "invoice_number": "KOS-2611-042", "invoice_date": "2026-11-12", "po_number": None,
  "line_items": [{"description": "A4 copier paper 75 GSM (ream)", "quantity": 200, "unit_price": 245, "amount": 49000},
                 {"description": "Whiteboard markers (box of 10)", "quantity": 40, "unit_price": 380, "amount": 15200},
                 {"description": "Lever arch files", "quantity": 150, "unit_price": 96, "amount": 14400}],
  "subtotal": 78600, "tax_lines": [{"type": "CGST", "rate": 9, "amount": 7074}, {"type": "SGST", "rate": 9, "amount": 7074}],
  "total": 92748, "bank_account": "0472101034567", "ifsc": "CNRB0000472", "bank_name": "Canara Bank", "notes": []}
def mpost3(url, **k):
    mcalls.append(url.rsplit("/v1/", 1)[1])
    assert "Kaveri" in k["json"]["messages"][0]["content"]
    return G(200, {"choices": [{"message": {"content": json.dumps(scan_reply)}}]})
extract_llm.httpx.post = mpost3
res = run("02_edge_scanned_no_po_kaveri.pdf")
print("mistral scan:", mcalls, [c["code"] for c in res["stages"][1]["checks"]], res["decision"]["outcome"])
assert mcalls == ["chat/completions"] and "ENGINE_AGREE" in [c["code"] for c in res["stages"][1]["checks"]] and res["decision"]["outcome"] == "APPROVED"

# 12) OCR mode but OCR rate-limited -> falls back to our text for the chat call, still uses Mistral
os.environ["MISTRAL_USE_OCR"] = "1"; mcalls.clear()
def mpost4(url, **k):
    mcalls.append(url.rsplit("/v1/", 1)[1])
    if url.endswith("/ocr"):
        return G(429, {"message": "Rate limit exceeded"})
    return G(200, {"choices": [{"message": {"content": json.dumps(reply)}}]})
extract_llm.httpx.post = mpost4
store.reset()
res = run("01_happy_path_shree_ganesh.pdf")
print("ocr 429 -> text:", mcalls, res["engine"], res["decision"]["outcome"])
assert mcalls[-1] == "chat/completions" and res["engine"].startswith("mistral") and res["decision"]["outcome"] == "APPROVED"
del os.environ["MISTRAL_USE_OCR"]
print("ALL LLM PATH TESTS PASSED")
