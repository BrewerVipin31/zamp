# Demo script and interview prep

## Before recording / before the interview
1. Open the live link 2 minutes early (Render's free plan wakes up slowly).
2. Click **Reset demo data**. This matters: the duplicate and split-billing cases depend on the starting ledger, and the happy path will (correctly) be flagged as a duplicate if it was already approved.
3. Zoom the browser to 90% so all three columns fit on screen.
4. Have one invoice PDF from the `samples/` folder on your desktop to drag in live, so viewers see a real upload rather than only buttons.

## 5-minute video (target timings)

**0:00–0:30 · Framing**
"AP teams open every invoice PDF, find the PO, check the numbers and decide. I built a process that does that end to end. You give it an invoice, it runs seven stages, and it gives a decision of approve, hold or reject, with reasons, an owner and next steps. One design principle runs through it: AI reads the invoice, deterministic rules make the decision. So every outcome can be explained to an auditor, and a model mistake can't pay anyone."

**0:30–1:30 · Happy path** (drag in `01_happy_path…pdf`)
- Point at the stages as they tick: "Intake fingerprints the file and sees a text layer. Extraction reads 8 header fields and 2 lines. Data quality checks the maths, the date, the GSTIN checksum, and that it's billed to *our* GSTIN."
- "The vendor is matched on GSTIN, it's active, the bank account matches the master, and IGST is right because Pune to Bengaluru is inter-state."
- "There's no duplicate. PO-4501 matches line by line on price and quantity, and the GST rate matches the PO."
- Show the stamp. "Approved, ₹1,19,180. It's booked to the ledger, so the PO balance and the duplicate index update immediately."

**1:30–2:20 · Edge 3, disguised duplicate**
- "Same vendor sends the October cloud bill again. It has a different number format, with slashes and no leading zero, and it's a different file."
- Show the Duplicate stage: "I normalise invoice numbers, so `NCS/INV/2026/117` equals `NCS-INV-2026-0117`, which was paid on 12 Nov. Rejected." Open the drafted vendor email.
- "Notice the PO stage independently says the PO is already fully billed. That's corroborating evidence."

**2:20–3:10 · Edge 2, split billing**
- "PO for 10 pumps. 6 were billed on an earlier invoice. This one bills 5, so it's one pump beyond the PO."
- "The process tracks consumption across invoices, not just per invoice. It holds, routes to the PO owner in Plant Ops, and proposes releasing ₹2,36,000, the in-PO part, while holding ₹59,000."

**3:10–4:00 · Edge 4, bank change**
- "This one is perfect against the PO, which is exactly why it's dangerous. The remit-to account differs from the vendor master and the invoice says 'our bank details have changed'."
- "Held and routed to Treasury. And no email reply on purpose: if the vendor's email is compromised, replying goes to the fraudster. Verification is a call-back to the number held in the master."

**4:00–4:40 · Edge 1, scanned + no PO** (optional if time is short; otherwise mention it)
- "An image-only scan with no PO number. OCR reads it, and you can see a small OCR error where '2.' came out as 'Zi', but fuzzy line matching absorbs it."
- "It scores the vendor's two open POs on line overlap and amount fit (1.00 vs 0.29) and infers PO-4504. If the scores were close, it would hold instead of guessing."

**4:40–5:00 · Dashboard**
- "Every run is here: approved / held / rejected value, straight-through rate, exceptions grouped by owner, so a manager sees where the work is piling up."

## Live interview runbook
Run order: Reset → Happy → Edge 3 → Edge 2 → Edge 4 → Edge 1 → bonus cases if asked → re-run Happy (gets rejected as an exact duplicate, which is a nice closer) → Dashboard → Vendors & POs (show PO-4503 at 6/10 billed and the ledger).

If something breaks:
- LLM/API down: the app falls back to OCR + rules automatically and shows "LLM extraction: fell back". Say so; it's a feature.
- Wi-Fi gone: run locally with `EXTRACTOR=rules uvicorn app.main:app` from a terminal you prepared beforehand.

## Questions you'll likely get (and honest answers)

**Why not let the LLM make the decision?**
Approval is a policy question, and policy must be explainable and consistent. LLMs are excellent readers but non-deterministic deciders. So the LLM extracts into a strict schema, and rules with named reason codes decide. When both engines run, the LLM and OCR+rules readings are cross-checked; any disagreement on money, invoice number, PO or bank account triggers a hold.

**Why hold vs reject?**
Reject when paying would be wrong regardless of follow-up (duplicate, blocked or unknown vendor, wrong buyer). Hold when someone can fix it (Procurement amends the PO, the vendor reissues, Treasury verifies). Holds have an owner and a next action.

**How do you prevent the duplicate check from blocking legit invoices?**
There are three tiers:
- the exact same file → reject;
- the same normalised number from the same vendor → reject;
- the same amount within 30 days under a different number → *hold*, not reject, because recurring monthly bills of the same amount are legitimate.

**How is the PO inferred? Isn't that risky?**
Each open PO of that vendor is scored as 60% line overlap + 40% amount fit. The PO is accepted only if it scores ≥ 0.85 and is 0.2 clear of the runner-up; otherwise the invoice is held. It still shows as a "Note" so a reviewer can see the PO was inferred.

**Tolerances?**
- unit price +2%;
- amount over the remaining PO balance: up to 1% or ₹1,000, whichever is lower;
- rounding ₹1.

All of these are in `data/rules.json` and shown on the Vendors & POs page, and they're configurable per company.

**What's missing / what next?**
- 3-way match with goods receipts (right now "received" is assumed);
- reviewer actions with an audit trail;
- an email-inbox intake;
- ERP export;
- learning per-vendor layouts to raise confidence.

**Scale?**
Each invoice is independent, so the stages parallelise trivially. Rules mode is about 10 ms per digital invoice and under 1 s for a scan. An LLM call adds a few seconds. The pacing you see in the UI (`DEMO_PACING_MS`) is for watchability only.

**Why GSTIN checksum / tax type checks?**
A wrong GSTIN or the wrong tax type means the company can't claim input tax credit. That's real money AP teams in India care about, beyond whether the invoice matches the PO.

**What did you use AI for in building it?**
Be specific and honest: which parts you designed (the scenarios, the rules, hold vs reject), and which parts AI tools helped you build (the code, the test invoices).
