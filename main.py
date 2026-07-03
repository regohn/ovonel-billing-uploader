import base64
import io
import json
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
import pdfplumber
from google import genai
from google.genai import types

app = FastAPI(title="Ovonel Billing Uploader Runtime Engine")

# Strict extraction schema targeting Philippine BIR compliance structures
class BillingExtraction(BaseModel):
    vendor_name: str
    vendor_tin: str
    invoice_number: str
    customer_name: str
    customer_tin: str
    vatable_sales: float
    vat_amount: float
    total_amount_due: float
    bir_atp_no: Optional[str] = None

class InvoicePayload(BaseModel):
    base64_data: str
    mime_type: str

@app.post("/process-invoice", response_model=BillingExtraction)
async def process_invoice(payload: InvoicePayload):
    try:
        file_bytes = base64.b64decode(payload.base64_data)
        client = genai.Client() # Automatically searches for the GEMINI_API_KEY environment variable
        
        system_instruction = (
            "You are an expert financial auditor. Your task is to extract structural fields from the "
            "provided invoice text or image. Completely strip currency symbols, spaces, and commas from "
            "financial numerical outputs so they return as clean float data types. Clean minor formatting typos "
            "contextually (e.g., '10,00.00' is 10000.00)."
        )
        
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=BillingExtraction,
            temperature=0.0,
        )

        # PROFILE A: Process text layer locally for Native Digital PDFs (Token Saver Profile)
        if "pdf" in payload.mime_type.lower():
            extracted_text = []
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text: extracted_text.append(text)
            
            full_text = "\n".join(extracted_text)
            
            if full_text.strip():
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[system_instruction, f"Raw Invoice Text:\n\n{full_text}"],
                    config=config
                )
                return json.loads(response.text)

        # PROFILE B: Fallback to pure vision parsing for raw camera photos or scanned documents
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                system_instruction,
                types.Part.from_bytes(data=file_bytes, mime_type=payload.mime_type)
            ],
            config=config
        )
        return json.loads(response.text)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def home():
    return {"status": "Online", "project": "Ovonel Billing Uploader Standalone Server"}
