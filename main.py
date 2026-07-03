"""
Ovonel Billing Audit Pipeline — Render API (main.py)
=====================================================
Stateless FastAPI service deployed on Render (free tier).

Pipeline logic:
  1. Receive base64-encoded file + MIME type from Google Apps Script.
  2. If PDF → attempt native text extraction with pdfplumber (free, local).
       If text layer found → send text-only prompt to Gemini (cheap).
       If scanned/empty → fall through to multimodal vision route.
  3. If image (jpg/png/webp) OR scanned PDF → send raw binary to Gemini
     multimodal vision (slightly more tokens, still low cost).
  4. Gemini returns structured JSON → validate → return to caller.

Environment variables required (set in Render dashboard):
  GEMINI_API_KEY   — Your Google AI Studio API key.
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

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Ovonel Billing Extraction API",
    description="Hybrid PDF/image → Gemini structured extraction service.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Locked down by Google Apps Script origin anyway
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── Gemini client ─────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY not set — extraction calls will fail.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL  = "gemini-2.5-flash"

# ── Prompts ───────────────────────────────────────────────────────────────────
SYSTEM_INSTRUCTION = """You are an expert financial audit data extraction assistant
specialised in auditing logistics, transportation, and service invoices issued in the
Philippines.

Your sole responsibility is to extract critical metadata, compliance information, and
financial summaries from raw document text or images and return them as a clean JSON
object.

CRITICAL OPERATIONAL RULES:

1. DATA TYPES: Transform all financial numbers into pure float values.
   Strip currency symbols (₱, P, PHP, $), whitespace, footnotes, and
   formatting commas — e.g. "₱ 11,200.00" becomes 11200.00.

2. EXTRACTION TRUTH: Extract only data explicitly present in the document.
   Do not hallucinate or compute missing values unless applying DATA REPAIR below.

3. DATA REPAIR: If a financial sub-section has minor OCR issues (e.g. "10,00.00"
   under VATable Sales), apply standard Philippine VAT accounting logic to infer the
   true value using surrounding totals (VATable + 12% VAT = Total).

4. VENDOR vs CUSTOMER IDENTIFICATION (most critical rule):
   - The VENDOR (issuer/seller) is the company whose name and logo appears at the
     TOP of the invoice — typically in large bold text in the header. They are the
     one ISSUING the invoice. Their TIN appears near the top under their name,
     labelled "VAT Reg. TIN" or "TIN:". Extract this as vendor_name and vendor_tin.
   - The CUSTOMER (buyer/billed party) is found under the "SOLD TO" or "BILL TO"
     section. Their name appears next to "Registered Name:" and their TIN appears
     next to "TIN:". Extract this as customer_name and customer_tin.
   - NEVER use the cashier, signatory, representative, or "By:" field as the
     customer name. Names appearing under "Cashier", "Authorized Representative",
     "Prepared by", or "By:" are signatories only — ignore them for customer_name.

5. INVOICE NUMBER: Found near the top right of the invoice, labelled "No.", "Invoice
   No.", or "N°". It is usually a short numeric code (e.g. "0508"). Do NOT confuse
   it with BIR ATP numbers, booking references, or container numbers.

6. BIR ATP NUMBER: Found at the bottom left of the invoice, labelled "BIR Authority
   to Print No." or "BIR Auth. to Print No." — it is a long alphanumeric code
   starting with digits and letters (e.g. "052AU20260000001427"). Extract the full
   number exactly as printed.

7. TIN FORMAT: TINs must retain their hyphens (e.g. 123-456-789-000).

8. MISSING FIELDS: If a field cannot be found or inferred, return null for that key.
   Never omit a key from the response object.

9. OUTPUT FORMAT: Return ONLY a raw JSON object. No markdown fences, no explanation,
   no preamble. The response must be directly parseable by json.loads()."""

TEXT_PROMPT_TEMPLATE = """Analyse the following raw text extracted from a Philippine
service billing invoice. Extract and populate every field in the JSON schema below.

REMINDERS:
- vendor_name = company at the TOP of the invoice (the issuer/seller)
- customer_name = name under "SOLD TO" / "Registered Name" section
- Do NOT use cashier or signatory names as customer_name
- invoice_number = the short "No." or "N°" number near the top right
- bir_atp_no = the long BIR Authority to Print number at the bottom

[START OF RAW INVOICE TEXT]
{raw_text}
[END OF RAW INVOICE TEXT]

Return ONLY this JSON object with values filled in (use null for missing fields):
{{
  "invoice_number":    null,
  "bir_atp_no":        null,
  "vendor_name":       null,
  "vendor_tin":        null,
  "customer_name":     null,
  "customer_tin":      null,
  "vatable_sales":     null,
  "vat_amount":        null,
  "total_amount_due":  null
}}"""

VISION_PROMPT = """Analyse this Philippine service billing invoice image or scanned
document. Extract and populate every field in the JSON schema below.

CRITICAL REMINDERS:
- vendor_name = the company name printed largest at the TOP of the invoice (the issuer)
- customer_name = the name under the "SOLD TO" box next to "Registered Name:"
- Do NOT use the cashier, "By:", or "Authorized Representative" name as customer_name
- invoice_number = the short number after "No." or "N°" near the top right corner
- bir_atp_no = the long alphanumeric code after "BIR Authority to Print No." at the bottom left

Return ONLY this JSON object with values filled in (use null for missing fields):
{
  "invoice_number":    null,
  "bir_atp_no":        null,
  "vendor_name":       null,
  "vendor_tin":        null,
  "customer_name":     null,
  "customer_tin":      null,
  "vatable_sales":     null,
  "vat_amount":        null,
  "total_amount_due":  null
}"""

# ── Pydantic models ───────────────────────────────────────────────────────────
class ExtractRequest(BaseModel):
    file_data: str   # raw base64, no data-URI prefix
    mime_type: str   # e.g. "application/pdf" or "image/jpeg"


class BillingData(BaseModel):
    invoice_number:   str | None = None
    bir_atp_no:       str | None = None
    vendor_name:      str | None = None
    vendor_tin:       str | None = None
    customer_name:    str | None = None
    customer_tin:     str | None = None
    vatable_sales:    float | None = None
    vat_amount:       float | None = None
    total_amount_due: float | None = None

    # Coerce numeric strings → float; strip stray currency symbols
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
def extract_pdf_text(raw_bytes: bytes) -> str:
    """Attempt native text extraction from a PDF using pdfplumber."""
    text_parts = []
    try:
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
    except Exception as exc:
        log.warning("pdfplumber extraction failed: %s", exc)
    return "\n".join(text_parts).strip()


def call_gemini_text(raw_text: str) -> dict:
    """Send extracted text to Gemini and return parsed JSON."""
    prompt = TEXT_PROMPT_TEMPLATE.format(raw_text=raw_text)
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return json.loads(response.text)


def call_gemini_vision(raw_bytes: bytes, mime_type: str) -> dict:
    """Send raw binary to Gemini multimodal vision and return parsed JSON."""
    # Gemini SDK accepts inline binary via Part
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
    """Ensure all required keys are present (default to None)."""
    required = [
        "invoice_number", "bir_atp_no",
        "vendor_name",    "vendor_tin",
        "customer_name",  "customer_tin",
        "vatable_sales",  "vat_amount",  "total_amount_due",
    ]
    return {k: raw.get(k) for k in required}


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "service": "Ovonel Billing Extraction API v2.0"}


@app.post("/extract", response_model=BillingData)
def extract(req: ExtractRequest):
    """
    Main extraction endpoint.
    Accepts base64 file + mime_type, returns structured BillingData JSON.
    """
    # Decode base64
    try:
        raw_bytes = base64.b64decode(req.file_data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 payload.")

    log.info("Received file: mime=%s  size=%d bytes", req.mime_type, len(raw_bytes))

    raw_dict: dict | None = None

    # ── Route A: PDF → always vision for layout-aware extraction ────────────
    if req.mime_type == "application/pdf":
        log.info("PDF received — using multimodal vision for layout accuracy.")
        try:
            raw_dict = call_gemini_vision(raw_bytes, "application/pdf")
            log.info("Gemini vision route (PDF) succeeded.")
        except Exception as exc:
            log.error("Gemini vision route failed: %s", exc)
            raise HTTPException(
                status_code=502,
                detail=f"AI extraction failed for PDF: {exc}"
            )

    # ── Route C: Image upload → vision ───────────────────────────────────────
    elif req.mime_type.startswith("image/"):
        log.info("Image file — using multimodal vision route.")
        try:
            raw_dict = call_gemini_vision(raw_bytes, req.mime_type)
            log.info("Gemini vision route (image) succeeded.")
        except Exception as exc:
            log.error("Gemini vision route failed: %s", exc)
            raise HTTPException(
                status_code=502,
                detail=f"AI extraction failed for image: {exc}"
            )

    else:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported mime type: {req.mime_type}"
        )

    # ── Validate and return ───────────────────────────────────────────────────
    clean = sanitise_response(raw_dict)
    log.info("Extraction complete: %s", clean)

    try:
        return BillingData(**clean)
    except Exception as exc:
        log.error("Response validation error: %s  raw=%s", exc, clean)
        raise HTTPException(status_code=500, detail=f"Response schema error: {exc}")
