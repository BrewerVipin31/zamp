# AP Autopilot: invoice PDF to payment decision

A working accounts-payable process (Zamp ASA case study, **PS-1 Invoice processing**). You give it a vendor invoice (a digital PDF, a scanned PDF or a photo). It reads the invoice, validates it, checks the vendor, looks for duplicates and matches it to a purchase order. It ends with one of three decisions: **APPROVED**, **ON HOLD** or **REJECTED**. Every decision comes with its reasons, who it is routed to, the next action, and a drafted message to the vendor when the vendor needs to fix something.

The UI has three views:
- **Process invoice** shows a live run view: each of the 7 stages ticks through as it executes, then the decision appears with the extracted fields and the original file.
- **Dashboard** shows run history, approved / on-hold / rejected value, straight-through rate, average processing time, exceptions grouped by owner, and the top reasons for exceptions.
- **Vendors & POs** shows the reference data: the vendor master, each PO with its billed and remaining balance, the matching rules, and the invoice ledger.

---

## The process

```
Invoice (PDF / scan / photo)
   │
1  Intake ─────────── SHA-256 fingerprint, file type, text layer or scan?
2  Extraction ─────── LLM reads the document (Claude or Gemini)  ┐ two independent
                      OCR (Tesseract) + rules engine             ┘ readers, cross-checked
3  Data quality ───── mandatory fields · lines = subtotal · subtotal + tax = total ·
                      date sane · GSTIN checksum · billed to OUR GSTIN
4  Vendor ─────────── in vendor master? active? GSTIN matches? remit-to bank = master?
                      GST type fits place of supply (CGST+SGST vs IGST)
5  Duplicates ─────── same file hash · same invoice no. after normalisation ·
                      same amount within 30 days · already in the exception queue
6  PO match ───────── explicit PO, or infer one from the vendor's open POs ·
                      line match · price tolerance · qty vs PO incl. earlier invoices ·
                      remaining PO value · GST rate and amount vs PO
7  Decision ───────── any REJECT → REJECTED; else any HOLD → ON HOLD; else APPROVED
                      + routing, next action, vendor email, payable amount, ledger update
```

**Core design choice: AI reads, rules decide.** The LLM handles the messy part, which is reading invoices in any layout, including scans. Every decision is a deterministic rule with a named reason code. That keeps each outcome explainable to an auditor and repeatable, and a model hallucination can never cause a payment on its own:
- if the LLM and OCR disagree on the total, invoice number, PO or bank account, the invoice goes **ON HOLD**;
- if the LLM is down, the process falls back to OCR + rules and keeps running.

Holds and rejects are deliberately different:
- **REJECTED** means paying is wrong: a duplicate, a blocked or unknown vendor, or an invoice addressed to another company.
- **ON HOLD** means someone has to act before payment: Procurement, Treasury or the vendor.

## Scenarios (in the app under "Test invoices")

| # | Scenario | What makes it hard | Decision |
|---|---|---|---|
| Happy | Shree Ganesh Packaging, PO-4501 | Clean match, 2 lines, inter-state IGST | APPROVED ₹1,19,180 |
| Edge 1 | Kaveri Office, **scanned, no PO number** | No text layer (OCR misreads some characters, e.g. "Zi" for "2."). The PO has to be **inferred** from 2 open POs by line overlap and amount fit (scores 1.00 vs 0.29) | APPROVED, with the inferred PO noted |
| Edge 2 | Apex Industrial, **split billing** | PO-4503 is for 10 pumps. 6 are already billed and paid; this invoice bills 5 more, so it runs 1 pump past the PO. The process proposes releasing the ₹2,36,000 that is within the PO and holding ₹59,000 | ON HOLD → PO owner |
| Edge 3 | Nimbus Cloud, **disguised duplicate** | `NCS/INV/2026/117` vs the already-paid `NCS-INV-2026-0117`: a different file and different formatting, but the same bill. Number normalisation catches it | REJECTED + "already processed" email |
| Edge 4 | Shree Ganesh, **bank details changed** | Everything matches the PO, but the remit-to account differs from the vendor master and the invoice says "our bank details have changed" (the classic BEC / invoice-fraud pattern) | ON HOLD → Treasury. Call-back verification; no email reply |
| Bonus | Nimbus, missing invoice number | Can't be booked or duplicate-checked | ON HOLD + vendor request email |
| Bonus | Rapid Route, blocked vendor | PO matches perfectly but the vendor failed KYC | REJECTED |

Also handled: re-running any approved invoice is caught as a duplicate (exact file hash). Re-submitting an invoice that is already on hold is flagged. Unreadable files are held with a re-send request. PNG/JPG photos go through OCR.

## Run locally

```bash
# needs Python 3.12+, plus Tesseract and Poppler:
#   macOS:  brew install tesseract poppler
#   Ubuntu: sudo apt install tesseract-ocr poppler-utils
#   Windows: easiest is Docker (below)
pip install -r requirements.txt
uvicorn app.main:app --port 8000
# open http://localhost:8000
```

Or with Docker: `docker build -t ap-autopilot . && docker run -p 8000:8000 ap-autopilot`

### Turn on the LLM extractor (optional, recommended for the demo)
Set **one** of these environment variables before starting (if several are set, Mistral is used first, then Claude, then Gemini):
- `MISTRAL_API_KEY=...` (free "Experiment" plan at console.mistral.ai). Uses Mistral OCR (`mistral-ocr-latest`) to read the document, then `mistral-small-latest` to produce the JSON. Override with `MISTRAL_OCR_MODEL` / `MISTRAL_MODEL`.
- `GEMINI_API_KEY=...` (free key from Google AI Studio). If the configured model is retired or overloaded, the app retries and then fails over to another Flash model the key can use.
- `ANTHROPIC_API_KEY=...`. Default model is `claude-haiku-5-5`; override it with `ANTHROPIC_MODEL`.

With no key, the app runs fully offline on OCR + rules (all scenarios pass). With a key, the badge in the top bar changes, and every run shows an "LLM vs OCR cross-check" line. `EXTRACTOR=rules` forces offline mode, which is a safe fallback if the Wi-Fi fails mid-interview.

Other settings:
- `DEMO_PACING_MS` (default 450) is a minimum on-screen time per stage, so the live view is watchable. Real processing time is shown per stage. Set it to 0 for raw speed.
- `RUNTIME_DIR` is where run history, uploads and the ledger are stored.

## Deploy (live, runnable link)

**Render (free):**
1. Push this folder to a GitHub repo.
2. In Render, go to New → Blueprint and pick the repo. It reads `render.yaml`.
3. Optionally add `GEMINI_API_KEY` in Environment.
4. You get `https://ap-autopilot-xxxx.onrender.com`.

On the free plan the service sleeps after 15 minutes idle, and the first request takes about 1 minute. **Open the link 2 minutes before the interview.** Storage is ephemeral (a restart clears history), which is fine because you press *Reset demo data* before demoing anyway.

**Hugging Face Spaces (free, doesn't sleep as aggressively):**
1. Create a Space → SDK: Docker → upload these files.
2. Add the following at the very top of `README.md`:
   ```
   ---
   title: AP Autopilot
   sdk: docker
   app_port: 7860
   ---
   ```
3. Add a Space variable `PORT=7860` and, optionally, secret `GEMINI_API_KEY`.

## Tests
```bash
python tests/run_all.py -v        # every scenario → expected decision, plus duplicate-on-rerun
python tests/test_llm_paths.py    # LLM failure fallback, mocked LLM success, LLM/OCR disagreement → hold
python tools/generate_samples.py  # regenerate the test invoices
```

## Assumptions (stated, so they can be challenged)
- The buyer is one Indian entity (Meridian Foods, Karnataka GSTIN). All amounts are INR, and GST is IGST for inter-state supplies and CGST+SGST for intra-state ones.
- **2-way match** (invoice ↔ PO). There is no goods-receipt data, so "received" is assumed for anything on the PO. A 3-way match with GRN is the first extension.
- Tolerances:
  - unit price: +2% over PO;
  - amount: up to 1% or ₹1,000 over the remaining PO value, whichever is lower;
  - rounding: ₹1.
  
  Underbilling is accepted.
- A PO is inferred only when the best match scores ≥ 0.85 and is at least 0.2 ahead of the next PO. Otherwise the invoice is held rather than guessed.
- The invoice ledger (paid and approved invoices) is the source of truth for PO consumption and duplicates. Approved runs are booked into it straight away.
- Bank detail changes are never accepted from an invoice. They need call-back verification through the contact held in the vendor master.

## What I'd build next
3-way match with GRNs · email-inbox intake (forwarding address) · reviewer actions in the UI (approve the hold, release partial) with audit log · per-vendor learned layouts and confidence calibration · ERP export (Tally / SAP) · SLA timers on exceptions.

## Code map
```
app/main.py            FastAPI: runs, live SSE stage stream, history, reference data, reset
app/pipeline.py        the 7 stages and all decision rules
app/extract_llm.py     Mistral / Claude / Gemini document extraction (strict JSON, nulls for missing)
app/extract_rules.py   offline extractor for text-layer and OCR text
app/gstin.py           GSTIN format + checksum
app/store.py           vendor master, POs, rules, ledger, SQLite run history
app/static/            UI (vanilla HTML/CSS/JS)
data/                  company, vendors, POs, rules, seeded invoice history
samples/               test invoices + manifest
tools/generate_samples.py
tests/
```
