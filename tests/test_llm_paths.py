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
print("ALL LLM PATH TESTS PASSED")
