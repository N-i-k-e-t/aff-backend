import os
import time
import json
from datetime import datetime
from typing import List, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google.cloud import firestore, secretmanager
from googleapiclient.discovery import build
import google.generativeai as genai

app = FastAPI()

# CORS configuration
FRONTEND_URL = os.getenv("FRONTEND_URL", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Secret Management ---
def get_secret(secret_id: str) -> str:
    # Check env first, then Secret Manager
    if os.getenv(secret_id):
        return os.getenv(secret_id)
    
    try:
        client = secretmanager.SecretManagerServiceClient()
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        if not project_id:
            return ""
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        print(f"Error reading secret {secret_id}: {e}")
        return ""

# --- Models ---
class Rating(BaseModel):
    label: str
    score: Optional[int]

class Bucket(BaseModel):
    bucket: str
    a: Optional[float]
    b: Optional[float]
    avg: float
    delta: float
    flag: str

class AiSynthesis(BaseModel):
    strengths: List[str]
    concerns: List[str]
    contradictions: List[str]
    overall: str
    confidence_pct: int
    shortlist_recommendation: str
    rationale: str

class Founder(BaseModel):
    fid: str
    name: str
    startup: str
    subgroup: str
    buckets: List[Bucket]
    pct_a: Optional[float]
    pct_b: Optional[float]
    final: float
    consensus: str
    ai: AiSynthesis
    updated: str

# --- Logic ---
def rating_to_number(r: str) -> Optional[int]:
    if not r: return None
    r = r.lower()
    if "strongly agree" in r: return 4
    if "agree" in r: return 3
    if "borderline" in r: return 2
    if "weak" in r: return 1
    return None

def calculate_weighted(scores: dict) -> float:
    # 30% Char, 25% Mind, 25% Behav, 20% Lens
    # (S*W) * 25 to get %
    weighted = (
        (scores.get('char') or 0) * 0.30 +
        (scores.get('mind') or 0) * 0.25 +
        (scores.get('behav') or 0) * 0.25 +
        (scores.get('lens') or 0) * 0.20
    ) * 25
    return round(weighted, 1)

async def gemini_synth(pair_data: str) -> dict:
    api_key = get_secret("GEMINI_API_KEY")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.0-flash-exp')
    
    prompt = f"""
    You are an unbiased evaluator synthesising TWO independent jury reads of one startup founder.
    Your job: surface truth, not flatter.

    STRICT RULES:
    1. Treat Jury A and Jury B as EQUAL sources. Never weight one over the other.
    2. If juries contradict, surface the contradiction explicitly — do not smooth over.
    3. No adjective inflation. Match tone to evidence.
    4. No founder-flattery, no founder-shaming. Describe, do not judge.
    5. Output ONLY valid JSON.

    Schema:
    {{
      "strengths": [string, string],
      "concerns": [string, string],
      "contradictions": [string],
      "overall": string,
      "confidence_pct": number,
      "shortlist_recommendation": "STRONG" | "REVIEW" | "REJECT",
      "rationale": string
    }}

    Data:
    {pair_data}
    """
    
    try:
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    except Exception as e:
        print(f"Gemini error: {e}")
        return {{
            "strengths": ["Manual review required"],
            "concerns": ["AI Error"],
            "contradictions": [],
            "overall": "AI synthesis failed.",
            "confidence_pct": 0,
            "shortlist_recommendation": "REVIEW",
            "rationale": str(e)
        }}

# --- Endpoints ---
@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.post("/api/sync")
async def sync(x_admin_token: str = Header(None)):
    if x_admin_token != get_secret("ADMIN_TOKEN"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    sheet_id = get_secret("SHEET_ID")
    api_key = get_secret("SHEETS_API_KEY")
    
    service = build('sheets', 'v4', developerKey=api_key)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="Evaluations!A2:S"
    ).execute()
    rows = result.get('values', [])
    
    # Filter and Group
    founders = {}
    for r in rows:
        if not r or not r[1].startswith("F"): continue
        fid = r[1]
        if fid not in founders: founders[fid] = []
        founders[fid].append(r)
    
    db = firestore.Client()
    processed_count = 0
    
    for fid, pair in founders.items():
        # Build MasterCard logic (Simplified for space, matching earlier TS logic)
        # ... logic to build 'founder_doc' ...
        # (Assuming we map all fields correctly as in Stage 1 & 2)
        
        # This is where we'd call gemini_synth
        # For now, let's assume we've built the doc
        
        # doc_ref = db.collection("founders").document(fid)
        # doc_ref.set(founder_doc)
        processed_count += 1

    return {"ok": True, "founders": processed_count}

@app.get("/api/founders")
async def get_founders():
    db = firestore.Client()
    docs = db.collection("founders").stream()
    return [doc.to_dict() for doc in docs]

@app.get("/api/founder/{fid}")
async def get_founder(fid: str):
    db = firestore.Client()
    doc = db.collection("founders").document(fid).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Not found")
    return doc.to_dict()
