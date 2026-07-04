"""
Ovonel Billing Audit Pipeline — Render API (main.py) v2.2
==========================================================
Changes in v2.2:
  - Added vendor_address and customer_address extraction
  - Returns file_name in response for Drive upload tracking
"""

import base64
import io
import json
import logging
import os
import re

import pdfplumber
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from pydantic import BaseModel, field_validator

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Ovonel Billing Extraction API", version="2.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["POST","GET"], allow_headers=["*"])

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY not set.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL  = "gemini-2.5-flash"

# ── Prompts ───────────────────────────────────────────────────────────────────
SYSTEM_INSTRUCTION = """You are a precise financial document parser for Philippine VAT invoices.
Return ONLY a raw JSON object — no markdown, no explanation, no extra text."""

VISION_PROMPT = """You are parsing a Philippine SERVICE INVOICE. Extract exactly these 11 fields.

FIELD-BY-FIELD INSTRUCTIONS:

invoice_number:
  - Location: TOP RIGHT corner, next to "No." or "Nº" or "Invoice No."
  - SHORT number only — typically 3 to 6 digits (e.g. "0508")
  - REJECT anything with dashes, letters, or longer than 8 characters
  - WRONG: "OTTO-26-0097", "P02B E 10062" — CORRECT: "0508"

bir_atp_no:
  - Location: BOTTOM LEFT corner, after "BIR Authority to Print No."
  - Long alphanumeric code e.g. "052AU20260000001427"

vendor_name:
  - Company in LARGEST TEXT at the very TOP of the document (the issuer/seller)
  - e.g. "OVONEL TRAD TRADERS OPC"

vendor_tin:
  - TIN of the VENDOR near the top header, labelled "VAT Reg. TIN"
  - e.g. "636-900-867-00000" — retain hyphens

vendor_address:
  - Full address of the VENDOR printed under their name in the header
  - Include room/unit, building, street, city, and any other address details
  - e.g. "Rm 218 Palmaris Bldg, Arista Place Condominium J.P. Rizal Street, Santo Niño 1704 City of Parañaque NCR, Fourth District Philippines"

customer_name:
  - Inside the "SOLD TO" box, next to "Registered Name:"
  - The BUYER — NOT the cashier or signatory
  - e.g. "Royal Cargo Inc" — WRONG: "Edwin A. Guisihan"

customer_tin:
  - TIN inside the "SOLD TO" box on the "TIN:" line
  - e.g. "200-342-283-00000"

customer_address:
  - Full address inside the "SOLD TO" box, labelled "Business Address:"
  - e.g. "RC Bldg, Sta Agueda Avenue, Pascor Drive, Brgy Santo Nino, Parañaque City Philippines 1704"

vatable_sales:
  - Amount labelled "VATable Sales" — strip ₱ and commas, return as float
  - e.g. 10000.0

vat_amount:
  - Amount labelled "VAT" or "Add: VAT" — return as float
  - e.g. 1200.0

total_amount_due:
  - Amount labelled "TOTAL AMOUNT DUE" — return as float
  - e.g. 11200.0

Return ONLY this JSON (use null for any field you cannot find):
{
  "invoice_number":    null,
  "bir_atp_no":        null,
  "vendor_name":       null,
  "vendor_tin":        null,
  "vendor_address":    null,
  "customer_name":     null,
  "customer_tin":      null,
  "customer_address":  null,
  "vatable_sales":     null,
  "vat_amount":        null,
  "total_amount_due":  null
}"""

TEXT_PROMPT_TEMPLATE = """You are parsing a Philippine SERVICE INVOICE from extracted text.

invoice_number: SHORT number (3-6 digits) next to "No." at top right. NOT dashed references like "OTTO-26-0097".
bir_atp_no: Long code after "BIR Authority to Print No." at bottom left.
vendor_name: Company in LARGEST text at TOP (issuer). e.g. "OVONEL TRAD TRADERS OPC"
vendor_tin: TIN labelled "VAT Reg. TIN" near top header. Retain hyphens.
vendor_address: Full address of vendor printed under their name in the header.
customer_name: Name next to "Registered Name:" in "SOLD TO" box. NOT the cashier.
customer_tin: TIN inside "SOLD TO" box on "TIN:" line.
customer_address: Full address inside "SOLD TO" box labelled "Business Address:".
vatable_sales, vat_amount, total_amount_due: float values, strip ₱ and commas.

[START OF RAW INVOICE TEXT]
{raw_text}
[END OF RAW INVOICE TEXT]

Return ONLY this JSON (use null for missing fields):
{{
  "invoice_number":    null,
  "bir_atp_no":        null,
  "vendor_name":       null,
  "vendor_tin":        null,
  "vendor_address":    null,
  "customer_name":     null,
  "customer_tin":      null,
  "customer_address":  null,
  "vatable_sales":     null,
  "vat_amount":        null,
  "total_amount_due":  null
}}"""

# ── Pydantic models ───────────────────────────────────────────────────────────
class ExtractRequest(BaseModel):
    file_data: str
    mime_type: str
    file_name: str = "invoice"

class BillingData(BaseModel):
    invoice_number:   str | None = None
    bir_atp_no:       str | None = None
    vendor_name:      str | None = None
    vendor_tin:       str | None = None
    vendor_address:   str | None = None
    customer_name:    str | None = None
    customer_tin:     str | None = None
    customer_address: str | None = None
    vatable_sales:    float | None = None
    vat_amount:       float | None = None
    total_amount_due: float | None = None

    @field_validator("vatable_sales", "vat_amount", "total_amount_due", mode="before")
    @classmethod
    def clean_currency(cls, v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        cleaned = re.sub(r"[₱P$,\s]", "", str(v))
        try:
            return float(cleaned)
        except ValueError:
            return None

# ── Helpers ───────────────────────────────────────────────────────────────────
def call_gemini_vision(raw_bytes: bytes, mime_type: str) -> dict:
    image_part = types.Part.from_bytes(data=raw_bytes, mime_type=mime_type)
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[image_part, VISION_PROMPT],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return json.loads(response.text)

def sanitise_response(raw: dict) -> dict:
    required = [
        "invoice_number", "bir_atp_no",
        "vendor_name",    "vendor_tin",    "vendor_address",
        "customer_name",  "customer_tin",  "customer_address",
        "vatable_sales",  "vat_amount",    "total_amount_due",
    ]
    return {k: raw.get(k) for k in required}

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "service": "Ovonel Billing Extraction API v2.2"}

@app.post("/extract", response_model=BillingData)
def extract(req: ExtractRequest):
    try:
        raw_bytes = base64.b64decode(req.file_data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 payload.")

    log.info("File: %s  mime=%s  size=%d bytes", req.file_name, req.mime_type, len(raw_bytes))

    if req.mime_type == "application/pdf" or req.mime_type.startswith("image/"):
        try:
            raw_dict = call_gemini_vision(raw_bytes, req.mime_type)
            log.info("Gemini vision succeeded.")
        except Exception as exc:
            log.error("Gemini vision failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"AI extraction failed: {exc}")
    else:
        raise HTTPException(status_code=415, detail=f"Unsupported mime type: {req.mime_type}")

    clean = sanitise_response(raw_dict)
    log.info("Extraction complete: %s", clean)

    try:
        return BillingData(**clean)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Schema error: {exc}")
