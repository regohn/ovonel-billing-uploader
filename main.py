"""
Ovonel Billing Audit Pipeline — Render API (main.py) v2.5.1
==========================================================
Changes:
  - Added 'date' extraction to Pydantic schema
  - Returns date, vendor_address, and customer_address in extraction payload
  - Zero-shot prompting directly linked to JSON schema mapping
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

app = FastAPI(title="Ovonel Billing Extraction API", version="2.5.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["POST","GET"], allow_headers=["*"])

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY not set.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL  = "gemini-2.5-flash"

# ── Prompts ───────────────────────────────────────────────────────────────────
SYSTEM_INSTRUCTION = """You are an expert financial auditor parsing Philippine VAT invoices.
Extract fields exactly as they are printed on the document. Do not extrapolate, guess, or hallucinate."""

VISION_PROMPT = """Analyze this billing document and extract the following parameters:
- invoice_number: The unique identifier number of the invoice (found next to 'No' or 'Invoice No')
- date: The date of the invoice (formatted as MM/DD/YYYY if possible)
- bir_atp_no: The BIR Authority to Print number (found in the footer or margins)
- vendor_name: Full registered business name of the issuer/vendor
- vendor_tin: Registered TIN of the vendor (including branch code suffix if present)
- vendor_address: Physical registered business address of the vendor
- customer_name: Full registered business name of the customer
- customer_tin: Registered TIN of the customer (including branch code suffix if present)
- customer_address: Physical registered billing address of the customer
- vatable_sales: Total sales amount Net of VAT (numerical value only)
- vat_amount: Value Added Tax (12%) amount (numerical value only)
- total_amount_due: Total amount due inclusive of VAT (numerical value only)

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
    return {"status": "ok", "service": "Ovonel Billing Extraction API v2.5.1"}

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


File 2: Google Apps Script Backend (Code.gs)

Paste this code in your Google Apps Script editor, replacing your existing Code.gs.

// ============================================================
// Ovonel Billing Audit Pipeline — Code.gs (v2.6)
// Fully Mapped with 16-Column physical sheet layout
// ============================================================
// Script Properties required:
//   RENDER_API_URL  → https://ovonel-billing-uploader.onrender.com/extract
//   SHEET_NAME      → Sheet1
//   SPREADSHEET_ID  → your billing sheet ID
//   DRIVE_FOLDER_ID → Google Drive folder ID for uploaded PDFs
// ============================================================

const PROPS           = PropertiesService.getScriptProperties();
const RENDER_API_URL  = PROPS.getProperty('RENDER_API_URL');
const SHEET_NAME      = PROPS.getProperty('SHEET_NAME')      || 'Sheet1';
const SPREADSHEET_ID  = PROPS.getProperty('SPREADSHEET_ID');
const DRIVE_FOLDER_ID = PROPS.getProperty('DRIVE_FOLDER_ID');

// ── Serve the web app ─────────────────────────────────────────────────────────
function doGet() {
  return HtmlService
    .createHtmlOutputFromFile('index')
    .setTitle('Ovonel Billing Audit Pipeline')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

// ── Save uploaded file to Google Drive ───────────────────────────────────────
function saveFileToDrive(base64Data, mimeType, originalFileName, billingData) {
  try {
    if (!DRIVE_FOLDER_ID) throw new Error('DRIVE_FOLDER_ID property is missing.');
    const folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
    
    const ext = originalFileName.includes('.') ? originalFileName.split('.').pop() : 'pdf';
    let formattedDate = 'NO-DATE';
    if (billingData && billingData.date) {
      formattedDate = billingData.date.toString().replace(/[\/\s]/g, '-');
    }
    const vendorClean = billingData && billingData.vendor_name 
      ? normalizeStr_(billingData.vendor_name).toUpperCase().replace(/[^A-Z0-9]/g, '_').substring(0, 20) 
      : 'UNKNOWN_VENDOR';
    const invClean = billingData && (billingData.invoice_no || billingData.invoice_number)
      ? billingData.invoice_no || billingData.invoice_number
      : 'NO-INV';
      
    const finalName = formattedDate + '_' + vendorClean + '_' + invClean + '.' + ext;
    
    const fileBytes = Utilities.base64Decode(base64Data);
    const blob = Utilities.newBlob(fileBytes, mimeType, finalName);
    const file = folder.createFile(blob);
    
    const url = file.getUrl();
    // Return both fileUrl and url keys to guarantee frontend matches completely
    return { success: true, fileUrl: url, url: url, fileName: finalName };
  } catch(e) {
    return { success: false, error: e.message };
  }
}

// ── Process Payload via Render API Node ──────────────────────────────────────
function extractBillingData(base64Data, mimeType) {
  if (!RENDER_API_URL) {
    return { error: 'RENDER_API_URL script property is missing.' };
  }
  
  const payload = {
    file_data: base64Data,
    mime_type: mimeType,
    file_name: 'input_document'
  };
  
  const options = {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };
  
  try {
    const response = UrlFetchApp.fetch(RENDER_API_URL, options);
    const resCode  = response.getResponseCode();
    const resText  = response.getContentText();
    
    if (resCode !== 200) {
      return { error: 'API Exception (' + resCode + '): ' + resText };
    }
    return JSON.parse(resText);
  } catch(e) {
    return { error: 'Fetch failed: ' + e.message };
  }
}

// ── Append Row Data to Spreadsheet ───────────────────────────────────────────
// Fully Aligned with 16-Column physical sheet layout
function saveBillingRow(data) {
  try {
    if (!SPREADSHEET_ID) throw new Error('SPREADSHEET_ID property is missing.');
    const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const sheet = ss.getSheetByName(SHEET_NAME);
    if (!sheet) throw new Error('Sheet "' + SHEET_NAME + '" not found.');

    const invoiceNo = data.invoice_no || data.invoice_number || '';
    const dateVal = data.date || '';
    const vendorAddress = data.vendor_address || '';
    const customerAddress = data.customer_address || '';
    const birAtpNo = data.bir_atp_no || '';
    const driveUrl = data.driveUrl || '';

    // Duplicate Check on Column I (Invoice No. is Column Index 9)
    if (invoiceNo) {
      const lastRow = sheet.getLastRow();
      if (lastRow > 1) {
        const invColumnIndex = 9; 
        const values = sheet.getRange(2, invColumnIndex, lastRow - 1, 1).getValues();
        const isDuplicate = values.some(row => normalizeStr_(row[0]) === normalizeStr_(invoiceNo));
        if (isDuplicate) {
          return { success: false, error: 'Duplicate Invoice Number detected: ' + invoiceNo };
        }
      }
    }

    const timestamp = new Date();
    
    // Write exactly 16 columns matching physical sheet headers
    sheet.appendRow([
      timestamp,                            // Col A: Timestamp
      data.vendor_name,                     // Col B: Supplier Name
      data.vendor_tin,                      // Col C: Supplier TIN
      vendorAddress,                        // Col D: Supplier Address
      data.customer_name,                   // Col E: Customer Name
      data.customer_tin,                    // Col F: Customer TIN
      customerAddress,                      // Col G: Customer Address
      birAtpNo,                             // Col H: BIR ATP No.
      invoiceNo,                            // Col I: Invoice No.
      dateVal,                              // Col J: Date
      parseAmount_(data.vatable_sales),     // Col K: Vatable Sales
      parseAmount_(data.vat_amount),        // Col L: VAT Amount
      parseAmount_(data.total_amount_due),  // Col M: Total Amount Due
      'Unverified',                         // Col N: Status
      '',                                   // Col O: Placeholder
      driveUrl                              // Col P: File Link
    ]);
    
    return { success: true, sheetUrl: ss.getUrl() };
  } catch(e) {
    return { success: false, error: e.message };
  }
}

// ── Utilities ────────────────────────────────────────────────────────────────
function normalizeStr_(val) {
  return (val || '').toString().trim().toLowerCase();
}

function parseAmount_(val) {
  if (!val) return 0;
  return parseFloat(val.toString().replace(/[₱,\s]/g, '')) || 0;
}
