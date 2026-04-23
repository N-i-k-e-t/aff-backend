import os
import json
import asyncio
from datetime import datetime
from typing import List, Dict, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google.cloud import firestore, secretmanager
from googleapiclient.discovery import build
import google.generativeai as genai

app = FastAPI()

# --- CORS ---
FRONTEND_URL = os.getenv("FRONTEND_URL", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Secret Helper ---
def get_secret(secret_id: str) -> str:
    if os.getenv(secret_id):
        return os.getenv(secret_id)
    try:
        client = secretmanager.SecretManagerServiceClient()
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except:
        return ""

# --- Scoring Helpers ---
def rating_to_score(r: str) -> int:
    r = r.lower()
    if "strongly agree" in r: return 4
    if "agree" in r: return 3
    if "borderline" in r: return 2
    return 1

def calc_pct(char, mind, behav, lens) -> float:
    raw = (char * 0.30 + mind * 0.25 + behav * 0.25 + lens * 0.20)
    return round(raw * 25, 1)

# --- Gemini Logic ---
async def generate_synthesis(pair_data: str) -> dict:
    genai.configure(api_key=get_secret("GEMINI_API_KEY"))
    model = genai.GenerativeModel('gemini-2.0-flash-exp')
    prompt = f"Synthesise these two jury reads into a de-biased JSON summary. Rules: Surface contradictions, equal weight. Data: {pair_data}"
    try:
        res = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(res.text)
    except:
        return {"strengths": [], "concerns": [], "contradictions": [], "overall": "Synthesis error", "confidence_pct": 0, "shortlist_recommendation": "REVIEW", "rationale": "AI error"}

# --- Endpoints ---
@app.get("/api/health")
def health():
    return {"status": "ok", "cloud": "gcp"}

@app.post("/api/sync")
async def sync_data(x_admin_token: str = Header(None)):
    if x_admin_token != get_secret("ADMIN_TOKEN"):
        raise HTTPException(status_code=401)

    # 1. Fetch from Sheets
    sheet_id = get_secret("SHEET_ID")
    api_key = get_secret("SHEETS_API_KEY")
    service = build('sheets', 'v4', developerKey=api_key)
    rows = service.spreadsheets().values().get(spreadsheetId=sheet_id, range="Evaluations!A2:S").execute().get('values', [])
    
    # 2. Group by Founder
    founders = {}
    for r in rows:
        if len(r) < 5 or not r[1].startswith("F"): continue
        fid = r[1]
        if fid not in founders: founders[fid] = []
        founders[fid].append(r)

    db = firestore.Client()
    batch = db.batch()
    processed = 0

    for fid, pair in founders.items():
        if not pair: continue
        
        # Jury A
        a = pair[0]
        s_a = { "char": rating_to_score(a[6]), "mind": rating_to_score(a[8]), "behav": rating_to_score(a[10]), "lens": rating_to_score(a[12]) }
        pct_a = calc_pct(**s_a)
        
        # Jury B (optional)
        b = pair[1] if len(pair) > 1 else None
        pct_b = None
        buckets = []
        
        bucket_names = ["Founder Character", "Learning Mindset", "Builder Behaviour", "Builder Lens"]
        score_indices = [6, 8, 10, 12]
        
        for i, name in enumerate(bucket_names):
            idx = score_indices[i]
            val_a = rating_to_score(a[idx])
            val_b = rating_to_score(b[idx]) if b else None
            avg = (val_a + val_b) / 2 if val_b else val_a
            delta = abs(val_a - val_b) if val_b else 0
            buckets.append({
                "bucket": name, "a": float(val_a), "b": float(val_b) if val_b else None,
                "avg": float(avg), "delta": float(delta), "flag": "SPLIT" if delta >= 2 else "OK"
            })

        if b:
            s_b = { "char": rating_to_score(b[6]), "mind": rating_to_score(b[8]), "behav": rating_to_score(b[10]), "lens": rating_to_score(b[12]) }
            pct_b = calc_pct(**s_b)

        final_score = (pct_a + pct_b) / 2 if pct_b else pct_a
        
        # Consensus
        sh_a = a[16].upper() if len(a) > 16 else ""
        sh_b = b[16].upper() if b and len(b) > 16 else ""
        consensus = "PENDING"
        if sh_a == "YES" and sh_b == "YES": consensus = "STRONG SHORTLIST"
        elif sh_a == "YES" or sh_b == "YES": consensus = "REVIEW"
        elif sh_a == "NO" and sh_b == "NO": consensus = "REJECT"

        # AI Synthesis
        ai = await generate_synthesis(str(pair))
        
        doc = {
            "fid": fid, "name": a[2], "startup": a[3], "subgroup": a[4],
            "buckets": buckets, "pct_a": pct_a, "pct_b": pct_b,
            "final": round(final_score, 1), "consensus": consensus,
            "ai": ai, "updated": datetime.now().isoformat(),
            "jury_a": {"jury": a[0], "notes": {"char": a[5], "mind": a[7], "behav": a[9], "lens": a[11]}, "signal": a[13], "doubt": a[14]},
            "jury_b": {"jury": b[0], "notes": {"char": b[5], "mind": b[7], "behav": b[9], "lens": b[11]}, "signal": b[13], "doubt": b[14]} if b else None
        }
        
        ref = db.collection("founders").document(fid)
        batch.set(ref, doc)
        processed += 1
    
    batch.commit()
    return {"ok": True, "processed": processed}

@app.get("/api/founders")
def get_all():
    db = firestore.Client()
    return [d.to_dict() for d in db.collection("founders").stream()]

@app.get("/api/founder/{fid}")
def get_one(fid: str):
    db = firestore.Client()
    doc = db.collection("founders").document(fid).get()
    if not doc.exists: raise HTTPException(status_code=404)
    return doc.to_dict()
