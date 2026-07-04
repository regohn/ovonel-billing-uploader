"""
Ovonel Billing Audit Pipeline — Render API (main.py) v2.5
==========================================================
Changes in v2.5:
  - Extracts 'date' cleanly from document
  - Extracts 'vendor_address' and 'customer_address'
  - Strict mapping for structured JSON schema outputs
"""

import base64
import io
import json
import logging
import os
import re

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from pydantic import BaseModel, field_validator

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Ovonel Billing Extraction API", version="2.5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["POST","GET"], allow_headers=["*"])

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY not set.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL  = "gemini-2.5-flash"

# ── Prompts ───────────────────────────────────────────────────────────────────
SYSTEM_INSTRUCTION = """You are a highly analytical auditing assistant. 
Extract the values exactly as printed on the document. Do not guess, make up, or modify addresses or dates."""

VISION_PROMPT = """Analyze this Philippine VAT invoice and extract these parameters:
- invoice_number: The unique invoice or bill number (found next to 'No' or 'Invoice No')
- date: The invoice issue date (formatted as MM/DD/YYYY)
- bir_atp_no: The Authority to Print number (found in the footer or margins)
- vendor_name: Full registered legal name of the selling vendor/supplier
- vendor_tin: Supplier TIN (Taxpayer Identification Number, including the branch suffix if printed)
- vendor_address: Full registered physical business address of the vendor/supplier
- customer_name: Full legal name of the customer (under 'SOLD TO' or 'BILLED TO')
- customer_tin: Customer TIN (including the branch suffix if printed)
- customer_address: Full billing or physical address of the customer

Return ONLY a raw JSON object matching the requested schema. Do not write markdown blocks."""

# ── Schemas ───────────────────────────────────────────────────────────────────
class ExtractRequest(BaseModel):
    file_data: str  # Base64 string
    mime_type: str
    file_name: str

class BillingData(BaseModel):
    invoice_number: str | None = None
    date: str | None = None
    bir_atp_no: str | None = None
    vendor_name: str | None = None
    vendor_tin: str | None = None
    vendor_address: str | None = None
    customer_name: str | None = None
    customer_tin: str | None = None
    customer_address: str | None = None
    vatable_sales: float | None = None
    vat_amount: float | None = None
    total_amount_due: float | None = None

    @field_validator("vatable_sales", "vat_amount", "total_amount_due", mode="before")
    @classmethod
    def clean_floats(cls, val):
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        s = re.sub(r"[^\d.-]", "", str(val))
        try:
            return float(s) if s else None
        except ValueError:
            return None

# ── LLM Pipeline ──────────────────────────────────────────────────────────────
def call_gemini_vision(raw_bytes: bytes, mime_type: str) -> dict:
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=raw_bytes, mime_type=mime_type),
                VISION_PROMPT
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=BillingData,
                temperature=0.1
            )
        )
        return json.loads(response.text)
    except Exception as e:
        log.error("GenAI Vision API error: %s", e)
        raise e

def parse_fallback_dict(raw: dict) -> dict:
    required = [
        "invoice_number", "date", "bir_atp_no",
        "vendor_name",    "vendor_tin",     "vendor_address",
        "customer_name",  "customer_tin",  "customer_address",
        "vatable_sales",  "vat_amount",    "total_amount_due",
    ]
    return {k: raw.get(k) for k in required}

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "service": "Ovonel Billing Extraction API v2.5"}

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
            return parse_fallback_dict(raw_dict)
        except Exception as exc:
            log.error("Gemini vision failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"AI extraction failed: {exc}")
    else:
        raise HTTPException(status_code=415, detail="Unsupported media type. Use PDF or image formats.")
