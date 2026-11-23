"""Generate realistic test invoices (different vendor layouts, one scanned) into samples/.

Run:  python tools/generate_samples.py
"""
import json
import random
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter
from reportlab.lib.colors import HexColor, black
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples"
W, H = A4

BUYER = {
    "name": "Meridian Foods Pvt Ltd",
    "addr": "42 Residency Road, Bengaluru, Karnataka 560025",
    "gstin": "29AAJCM4821K1Z3",
}


def inr(x: float) -> str:
    """Indian digit grouping: 119180.5 -> 1,19,180.50"""
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + "." + frac


def west(x: float) -> str:
    return f"{x:,.2f}"


def totals(lines, tax):
    sub = round(sum(q * p for _, q, p in lines), 2)
    taxes = [(label, round(sub * rate / 100, 2)) for label, rate in tax]
    return sub, taxes, round(sub + sum(t for _, t in taxes), 2)


# ---------------------------------------------------------------- templates
def template_classic(c, inv, fmt=inr):
    """Shree Ganesh / Rapid Route style: classic boxed tax invoice."""
    sub, taxes, total = totals(inv["lines"], inv["tax"])
    c.setFillColor(HexColor(inv.get("accent", "#1f3b6e")))
    c.rect(0, H - 28, W, 28, fill=1, stroke=0)
    c.setFillColor(HexColor("#ffffff"))
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, H - 19, "TAX INVOICE")
    c.setFillColor(black)
    y = H - 62
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, y, inv["vendor"]["name"])
    c.setFont("Helvetica", 9)
    for line in [inv["vendor"]["addr"], f"GSTIN: {inv['vendor']['gstin']}", f"Email: {inv['vendor']['email']}"]:
        y -= 13
        c.drawString(40, y, line)
    y -= 26
    c.setFont("Helvetica", 10)
    meta = []
    if inv.get("number"):
        meta.append(("Invoice No.:", inv["number"]))
    meta.append(("Invoice Date:", inv["date_str"]))
    if inv.get("po_text"):
        meta.append(("PO Number:", inv["po_text"]))
    meta.append(("Payment Terms:", inv.get("terms", "Net 30")))
    for k, v in meta:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(40, y, k)
        c.setFont("Helvetica", 10)
        c.drawString(140, y, v)
        y -= 14
    y -= 10
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Bill To:")
    c.setFont("Helvetica", 10)
    for line in [BUYER["name"], BUYER["addr"], f"GSTIN: {BUYER['gstin']}"]:
        y -= 13
        c.drawString(40, y, line)
    y -= 28
    c.setFillColor(HexColor("#e8edf5"))
    c.rect(36, y - 5, W - 72, 18, fill=1, stroke=0)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "#")
    c.drawString(60, y, "Description")
    c.drawRightString(360, y, "Qty")
    c.drawRightString(450, y, "Rate (Rs.)")
    c.drawRightString(555, y, "Amount (Rs.)")
    c.setFont("Helvetica", 9)
    for i, (desc, q, p) in enumerate(inv["lines"], 1):
        y -= 18
        c.drawString(40, y, str(i))
        c.drawString(60, y, desc)
        c.drawRightString(360, y, f"{q:g}")
        c.drawRightString(450, y, fmt(p))
        c.drawRightString(555, y, fmt(q * p))
    y -= 12
    c.line(36, y, W - 36, y)
    y -= 18
    rows = [("Sub Total", sub)] + [(f"{lbl}", amt) for lbl, amt in taxes]
    for k, v in rows:
        c.drawRightString(450, y, k)
        c.drawRightString(555, y, fmt(v))
        y -= 15
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(450, y - 2, "Grand Total")
    c.drawRightString(555, y - 2, fmt(total))
    y -= 50
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Bank Details")
    c.setFont("Helvetica", 9)
    b = inv["bank"]
    for line in [f"Bank: {b['bank_name']}", f"A/c No: {b['account_number']}", f"IFSC: {b['ifsc']}"]:
        y -= 13
        c.drawString(40, y, line)
    if inv.get("note"):
        y -= 22
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(HexColor("#9b1c1c"))
        c.drawString(40, y, inv["note"])
        c.setFillColor(black)
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(40, 50, "This is a computer generated invoice.")


def template_modern(c, inv, fmt=west):
    """Nimbus style: SaaS-like invoice, western number format, 'Bill #'."""
    sub, taxes, total = totals(inv["lines"], inv["tax"])
    y = H - 60
    c.setFont("Helvetica-Bold", 22)
    c.setFillColor(HexColor("#4b2fa6"))
    c.drawString(40, y, "INVOICE")
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 11)
    y -= 30
    c.drawString(40, y, inv["vendor"]["name"])
    c.setFont("Helvetica", 9)
    for line in [inv["vendor"]["addr"], f"GSTIN {inv['vendor']['gstin']}", inv["vendor"]["email"]]:
        y -= 12
        c.drawString(40, y, line)
    y -= 26
    c.setFont("Helvetica", 10)
    meta = []
    if inv.get("number"):
        meta.append(f"Bill # {inv['number']}")
    meta.append(f"Date: {inv['date_str']}")
    if inv.get("po_text"):
        meta.append(f"Customer PO: {inv['po_text']}")
    for m in meta:
        c.drawString(40, y, m)
        y -= 14
    y -= 10
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Billed To")
    c.setFont("Helvetica", 10)
    for line in [BUYER["name"], BUYER["addr"], f"GSTIN {BUYER['gstin']}"]:
        y -= 13
        c.drawString(40, y, line)
    y -= 30
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(HexColor("#4b2fa6"))
    c.drawString(40, y, "Item")
    c.drawRightString(380, y, "Qty")
    c.drawRightString(470, y, "Unit Price")
    c.drawRightString(555, y, "Line Total")
    c.setFillColor(black)
    c.line(40, y - 5, 555, y - 5)
    c.setFont("Helvetica", 9)
    for desc, q, p in inv["lines"]:
        y -= 18
        c.drawString(40, y, desc)
        c.drawRightString(380, y, f"{q:g}")
        c.drawRightString(470, y, "INR " + fmt(p))
        c.drawRightString(555, y, "INR " + fmt(q * p))
    y -= 26
    rows = [("Taxable Value", sub)] + taxes
    for k, v in rows:
        c.drawRightString(470, y, k)
        c.drawRightString(555, y, "INR " + fmt(v))
        y -= 15
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(440, y - 4, "Amount Due")
    c.drawRightString(555, y - 4, "INR " + fmt(total))
    y -= 50
    b = inv["bank"]
    c.setFont("Helvetica", 9)
    c.drawString(40, y, f"Pay to: {b['bank_name']} | Account Number {b['account_number']} | IFSC {b['ifsc']}")
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(40, 50, "Thank you for your business.")


def template_industrial(c, inv, fmt=inr):
    """Apex style: 'Invoice Number', DD/MM/YYYY, 'Against PO No.'"""
    sub, taxes, total = totals(inv["lines"], inv["tax"])
    y = H - 55
    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, y, inv["vendor"]["name"].upper())
    c.setFont("Helvetica", 9)
    for line in [inv["vendor"]["addr"], f"GSTIN/UIN: {inv['vendor']['gstin']}"]:
        y -= 12
        c.drawString(40, y, line)
    y -= 12
    c.line(40, y, 555, y)
    y -= 22
    c.setFont("Helvetica-Bold", 13)
    c.drawString(40, y, "Tax Invoice")
    y -= 22
    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Invoice Number: {inv['number']}")
    y -= 14
    c.drawString(40, y, f"Dated: {inv['date_str']}")
    y -= 14
    c.drawString(40, y, f"Against PO No. {inv['po_text']}")
    y -= 24
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Buyer:")
    c.setFont("Helvetica", 10)
    for line in [BUYER["name"], BUYER["addr"], f"GSTIN/UIN: {BUYER['gstin']}"]:
        y -= 13
        c.drawString(40, y, line)
    y -= 28
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Sl")
    c.drawString(60, y, "Description of Goods")
    c.drawRightString(360, y, "Quantity")
    c.drawRightString(450, y, "Rate")
    c.drawRightString(555, y, "Amount")
    c.setFont("Helvetica", 9)
    for i, (desc, q, p) in enumerate(inv["lines"], 1):
        y -= 18
        c.drawString(40, y, str(i))
        c.drawString(60, y, desc)
        c.drawRightString(360, y, f"{q:g} Nos")
        c.drawRightString(450, y, fmt(p))
        c.drawRightString(555, y, fmt(q * p))
    y -= 26
    for k, v in [("Total before tax", sub)] + taxes:
        c.drawRightString(450, y, k)
        c.drawRightString(555, y, fmt(v))
        y -= 15
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(450, y - 3, "Total Invoice Value")
    c.drawRightString(555, y - 3, fmt(total))
    y -= 50
    b = inv["bank"]
    c.setFont("Helvetica", 9)
    c.drawString(40, y, f"Company's Bank Details - Bank Name: {b['bank_name']}")
    c.drawString(40, y - 12, f"A/c No.: {b['account_number']}    Branch IFSC: {b['ifsc']}")
    if inv.get("note"):
        c.drawString(40, y - 30, inv["note"])


def template_stationer(c, inv, fmt=inr):
    """Kaveri style (gets scanned): 'Bill No', dd.mm.yyyy, no PO number."""
    sub, taxes, total = totals(inv["lines"], inv["tax"])
    y = H - 50
    c.setFont("Helvetica-Bold", 17)
    c.drawCentredString(W / 2, y, inv["vendor"]["name"])
    c.setFont("Helvetica", 9)
    y -= 14
    c.drawCentredString(W / 2, y, inv["vendor"]["addr"])
    y -= 12
    c.drawCentredString(W / 2, y, f"GSTIN : {inv['vendor']['gstin']}   Ph: 080-4123 5566")
    y -= 24
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(W / 2, y, "TAX INVOICE")
    y -= 26
    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Bill No : {inv['number']}")
    y -= 15
    c.drawString(40, y, f"Date : {inv['date_str']}")
    y -= 15
    c.drawString(40, y, inv.get("po_text_line", "Ref : As per your order"))
    y -= 24
    c.drawString(40, y, "To,")
    for line in [BUYER["name"], BUYER["addr"], f"GSTIN : {BUYER['gstin']}"]:
        y -= 14
        c.drawString(40, y, line)
    y -= 28
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "S.No")
    c.drawString(75, y, "Particulars")
    c.drawRightString(360, y, "Qty")
    c.drawRightString(450, y, "Rate")
    c.drawRightString(555, y, "Amount")
    c.line(40, y - 5, 555, y - 5)
    c.setFont("Helvetica", 10)
    for i, (desc, q, p) in enumerate(inv["lines"], 1):
        y -= 20
        c.drawString(40, y, f"{i}.")
        c.drawString(75, y, desc)
        c.drawRightString(360, y, f"{q:g}")
        c.drawRightString(450, y, fmt(p))
        c.drawRightString(555, y, fmt(q * p))
    y -= 12
    c.line(40, y, 555, y)
    y -= 20
    for k, v in [("Taxable Amount", sub)] + taxes:
        c.drawRightString(450, y, k)
        c.drawRightString(555, y, fmt(v))
        y -= 16
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(450, y - 3, "Net Payable")
    c.drawRightString(555, y - 3, fmt(total))
    y -= 46
    b = inv["bank"]
    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Bank : {b['bank_name']}")
    c.drawString(40, y - 15, f"A/C No : {b['account_number']}")
    c.drawString(40, y - 30, f"IFSC : {b['ifsc']}")
    c.drawRightString(555, 90, "For Kaveri Office Solutions")
    c.drawRightString(555, 60, "Authorised Signatory")


TEMPLATES = {
    "classic": template_classic,
    "modern": template_modern,
    "industrial": template_industrial,
    "stationer": template_stationer,
}

V = {
    "sgp": {"name": "Shree Ganesh Packaging Pvt Ltd", "addr": "Plot 17, MIDC Bhosari, Pune, Maharashtra 411026",
            "gstin": "27AAKCS3374M1ZQ", "email": "accounts@shreeganeshpack.example",
            "bank": {"bank_name": "HDFC Bank", "account_number": "50200012345678", "ifsc": "HDFC0000123"}},
    "ncs": {"name": "Nimbus Cloud Services LLP", "addr": "3rd Floor, 80 Feet Road, Koramangala, Bengaluru, Karnataka 560034",
            "gstin": "29AAHFN7712Q1ZI", "email": "billing@nimbuscloud.example",
            "bank": {"bank_name": "ICICI Bank", "account_number": "004105012345", "ifsc": "ICIC0000041"}},
    "ais": {"name": "Apex Industrial Supplies", "addr": "B-14, Sector 63, Noida, Uttar Pradesh 201301",
            "gstin": "09ABBFA5526H1ZK", "email": "finance@apexindustrial.example",
            "bank": {"bank_name": "State Bank of India", "account_number": "38123456789", "ifsc": "SBIN0001234"}},
    "kos": {"name": "Kaveri Office Solutions", "addr": "No. 9, Avenue Road, Bengaluru, Karnataka 560002",
            "gstin": "29AAGFK9043D1Z9", "email": "accounts@kaverioffice.example",
            "bank": {"bank_name": "Canara Bank", "account_number": "0472101034567", "ifsc": "CNRB0000472"}},
    "rrl": {"name": "Rapid Route Logistics Pvt Ltd", "addr": "Plot 5, Okhla Phase II, New Delhi, Delhi 110020",
            "gstin": "07AAOCR6650B1ZB", "email": "billing@rapidroute.example",
            "bank": {"bank_name": "Axis Bank", "account_number": "917020012345678", "ifsc": "UTIB0000917"}},
}

INVOICES = [
    {
        "file": "01_happy_path_shree_ganesh.pdf", "template": "classic", "vendor": V["sgp"],
        "number": "SGP/26-27/1187", "date_str": "14-Nov-2026", "po_text": "PO-4501",
        "lines": [("Corrugated boxes 5-ply 18x12x12 in", 2000, 42.50), ("Printed carton labels", 5000, 3.20)],
        "tax": [("IGST @ 18%", 18)],
        "label": "Happy path", "scenario": "Clean digital PDF, explicit PO, quantities and prices match PO-4501.",
        "expected": "APPROVED",
    },
    {
        "file": "02_edge_scanned_no_po_kaveri.pdf", "template": "stationer", "vendor": V["kos"], "scan": True,
        "number": "KOS-2611-042", "date_str": "12.11.2026",
        "lines": [("A4 copier paper 75 GSM (ream)", 200, 245.00), ("Whiteboard markers (box of 10)", 40, 380.00),
                  ("Lever arch files", 150, 96.00)],
        "tax": [("CGST @ 9%", 9), ("SGST @ 9%", 9)],
        "label": "Edge 1 - Scanned, no PO reference",
        "scenario": "Image-only scan (no text layer) and no PO number. Process must OCR it, then infer the right PO among the vendor's 2 open POs.",
        "expected": "APPROVED",
    },
    {
        "file": "03_edge_split_billing_overrun_apex.pdf", "template": "industrial", "vendor": V["ais"],
        "number": "AIS/2026/0907", "date_str": "19/11/2026", "po_text": "PO-4503",
        "lines": [("Hydraulic pump HP-200", 5, 50000.00)],
        "tax": [("IGST 18%", 18)],
        "label": "Edge 2 - Split billing overruns the PO",
        "scenario": "Second partial invoice on PO-4503. 6 of 10 pumps already billed and paid; this invoice bills 5 more, i.e. 1 pump beyond the PO.",
        "expected": "ON HOLD",
    },
    {
        "file": "04_edge_disguised_duplicate_nimbus.pdf", "template": "modern", "vendor": V["ncs"],
        "number": "NCS/INV/2026/117", "date_str": "November 3, 2026", "po_text": "PO-4502",
        "lines": [("Cloud hosting - October 2026", 1, 120000.00), ("Managed backup service - October 2026", 1, 18000.00)],
        "tax": [("CGST 9%", 9), ("SGST 9%", 9)],
        "label": "Edge 3 - Disguised duplicate",
        "scenario": "Re-sent invoice already paid on 12-Nov as NCS-INV-2026-0117. Different number formatting, different file, same bill.",
        "expected": "REJECTED",
    },
    {
        "file": "05_edge_bank_details_changed_shree_ganesh.pdf", "template": "classic", "vendor": V["sgp"],
        "bank": {"bank_name": "Yes Bank", "account_number": "012345678901", "ifsc": "YESB0000999"},
        "note": "IMPORTANT: Our bank details have changed. Please update your records and remit to the account above.",
        "number": "SGP/26-27/1203", "date_str": "18-Nov-2026", "po_text": "PO-4505",
        "lines": [("Stretch wrap film 23 micron (roll)", 300, 610.00)],
        "tax": [("IGST @ 18%", 18)],
        "label": "Edge 4 - Bank details changed (payment fraud signal)",
        "scenario": "Everything matches the PO perfectly, but the remit-to bank account differs from the vendor master - the classic invoice-fraud pattern.",
        "expected": "ON HOLD",
    },
    {
        "file": "06_bonus_missing_invoice_number_nimbus.pdf", "template": "modern", "vendor": V["ncs"],
        "number": None, "date_str": "November 16, 2026", "po_text": "PO-4507",
        "lines": [("SSL certificate - annual renewal", 4, 6500.00)],
        "tax": [("CGST 9%", 9), ("SGST 9%", 9)],
        "label": "Bonus - Missing invoice number",
        "scenario": "Invoice has no invoice number, so it cannot be booked or duplicate-checked. Process drafts a request to the vendor.",
        "expected": "ON HOLD",
    },
    {
        "file": "07_bonus_blocked_vendor_rapid_route.pdf", "template": "classic", "vendor": V["rrl"], "accent": "#7a3e00",
        "number": "RRL/1146", "date_str": "15-Nov-2026", "po_text": "PO-4506",
        "lines": [("Freight Bengaluru to Chennai (FTL)", 3, 28000.00)],
        "tax": [("IGST @ 12%", 12)],
        "label": "Bonus - Blocked vendor",
        "scenario": "Vendor exists but was blocked after failed KYC re-verification. Must not be paid regardless of PO match.",
        "expected": "REJECTED",
    },
]


def scanify(pdf_path: Path):
    """Turn a digital PDF into an image-only 'scan' (no text layer)."""
    rnd = random.Random(7)
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", "200", "-gray", "-png", str(pdf_path), f"{td}/p"], check=True)
        pages = []
        for png in sorted(Path(td).glob("p*.png")):
            im = Image.open(png).convert("L")
            im = im.rotate(0.7, resample=Image.BICUBIC, expand=False, fillcolor=255)
            px = im.load()
            w, h = im.size
            for _ in range(w * h // 300):  # speckle noise
                x, y = rnd.randrange(w), rnd.randrange(h)
                px[x, y] = rnd.choice([150, 190, 225])
            im = im.filter(ImageFilter.GaussianBlur(0.5))
            im = im.point(lambda v: min(255, int(v * 0.96 + 8)))  # paper tint
            pages.append(im.convert("RGB"))
        pages[0].save(pdf_path, "PDF", resolution=200, save_all=True, append_images=pages[1:])


def main():
    OUT.mkdir(exist_ok=True)
    manifest = []
    for inv in INVOICES:
        inv = {**inv}
        inv.setdefault("bank", inv["vendor"]["bank"])
        path = OUT / inv["file"]
        c = canvas.Canvas(str(path), pagesize=A4)
        c.setTitle(f"Invoice {inv.get('number') or ''}".strip())
        c.setAuthor(inv["vendor"]["name"])
        TEMPLATES[inv["template"]](c, inv)
        c.showPage()
        c.save()
        if inv.get("scan"):
            scanify(path)
        sub, taxes, total = totals(inv["lines"], inv["tax"])
        manifest.append({"file": inv["file"], "label": inv["label"], "scenario": inv["scenario"],
                         "expected": inv["expected"], "vendor": inv["vendor"]["name"], "total": total})
        print(f"{inv['file']:<50} total={total:>12,.2f}  expected={inv['expected']}")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
