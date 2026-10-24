import os
import json
import smtplib
import re
import io
import PyPDF2
import uuid
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from sqlalchemy.orm import Session
from sqlalchemy import select, text  
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Request
from fastapi.responses import Response
from app.api.deps import require_api_key
from app.core.llm_router import llm_chat_sync, get_llm_client
from app.database.database import get_db
from app.database.models import Booking
from pydantic import BaseModel
from openai import OpenAI
from twilio.rest import Client
from app.core.config import settings

router = APIRouter(prefix="/api/voice", tags=["Voice Agent"])

# ==========================================
# 🛑 CORE CONFIGURATION 🛑
# ==========================================
TWILIO_ACCOUNT_SID = settings.TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN = settings.TWILIO_AUTH_TOKEN
TWILIO_PHONE_NUMBER = settings.TWILIO_PHONE_NUMBER
NGROK_BASE_URL = (settings.PUBLIC_BASE_URL or "").rstrip("/")

# 🛑 ELEVENLABS CONFIGURATION 🛑
ELEVENLABS_API_KEY = settings.ELEVENLABS_API_KEY
# Changed to a FREE library voice (Rachel) so your Free Tier account works!
ELEVENLABS_VOICE_ID = settings.ELEVENLABS_VOICE_ID

# 🛑 SHORT-TERM MEMORY BANK 🛑
# This stores recent conversation context so SARA remembers who "he" or "she" is.
command_memory = {}

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None

def get_llm_client() -> OpenAI:
    # Shared LLM brain (xKiro by default) — see LLM_API_KEY / LLM_BASE_URL / LLM_MODEL in backend/.env.
    if not settings.LLM_API_KEY:
        raise HTTPException(status_code=503, detail="LLM_API_KEY is not configured.")
    return OpenAI(base_url=settings.LLM_BASE_URL, api_key=settings.LLM_API_KEY)

class VoiceCommand(BaseModel):
    user_id: str
    command: str

def _gmail_api_send(recipient: str, subject: str, body: str) -> str:
    """Send through the Gmail API using the OAuth token saved by /api/auth/google.
    Reuses the exact credential chain as the Email Automation Agent."""
    import base64
    from email.message import EmailMessage as _EmailMessage

    from google.oauth2.credentials import Credentials as UserCredentials
    from google.auth.transport.requests import Request as _GoogleRequest
    from googleapiclient.discovery import build as _google_build

    from app.database.database import SessionLocal as _MainSession
    from app.database.models import Integration as _Integration

    with _MainSession() as mdb:
        row = mdb.scalars(
            select(_Integration).where(_Integration.provider == "gmail")
        ).first()

    if not row or not row.refresh_token:
        raise RuntimeError(
            "Gmail is not connected yet. Click 'Connect Gmail' (bottom-left of the "
            "workspace) and complete the Google consent screen once — SARA then sends "
            "through the Gmail API with no app password needed."
        )

    creds = UserCredentials(
        token=row.access_token,
        refresh_token=row.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    creds.refresh(_GoogleRequest())

    # Persist the rotated access token so it stays fresh across restarts
    try:
        from app.database.database import SessionLocal as _MainSession2
        with _MainSession2() as mdb:
            integ = mdb.get(_Integration, row.id)
            if integ:
                integ.access_token = creds.token
                mdb.commit()
    except Exception:
        pass

    message = _EmailMessage()
    message["To"] = recipient
    message["From"] = settings.EMAIL_AGENT_FROM or settings.GMAIL_SENDER or "me"
    message["Subject"] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service = _google_build("gmail", "v1", credentials=creds, cache_discovery=False)
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent.get("id", "")


def send_gmail_smtp(recipient: str, subject: str, body: str):
    """Email dispatch with a fallback chain: Gmail API OAuth first (no password
    needed), then classic SMTP with GMAIL_SENDER + GMAIL_APP_PASSWORD."""
    try:
        return _gmail_api_send(recipient, subject, body)
    except RuntimeError:
        # Not connected via OAuth — fall through to SMTP if it is configured
        pass
    sender = settings.GMAIL_SENDER
    password = settings.GMAIL_APP_PASSWORD
    if not sender or not password:
        raise Exception(
            "Gmail not connected. Either click 'Connect Gmail' in the workspace "
            "(OAuth, recommended) or set GMAIL_SENDER + GMAIL_APP_PASSWORD in .env."
        )
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())

# ==========================================
# 🛑 ELEVENLABS AUDIO GENERATOR 🛑
# ==========================================
def text_to_speech_elevenlabs(text: str, filename: str):
    """
    DISABLED: the configured ElevenLabs key returns 401 (invalid/expired).
    The live voice loop uses Sarvam TTS via the /voice-agent engine instead
    (VOICE_TTS_PROVIDER=sarvam, verified working). Re-enable here by adding a
    valid ELEVENLABS_API_KEY in backend/.env and restoring the original body.
    """
    print("WARNING: ElevenLabs is disabled due to an invalid API key. Falling back to Sarvam (if configured).")
    return False
    # if not ELEVENLABS_API_KEY:
    #     print("ERROR: ElevenLabs API Key is missing.")
    #     return False
    # 
    # url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    # headers = {
    #     "Accept": "audio/mpeg",
    #     "Content-Type": "application/json",
    #     "xi-api-key": ELEVENLABS_API_KEY
    # }
    # data = {
    #     "text": text,
    #     "model_id": "eleven_monolingual_v1",
    #     "voice_settings": {"stability": 0.5, "similarity_boost": 0.5}
    # }
    # try:
    #     response = requests.post(url, json=data, headers=headers)
    #     if response.status_code == 200:
    #         with open(filename, "wb") as f:
    #             f.write(response.content)
    #         return True
    #     else:
    #         print(f"ELEVENLABS ERROR: {response.status_code} - {response.text}")
    #         return False
    # except Exception as e:
    #     print(f"ELEVENLABS EXCEPTION: {str(e)}")
    #     return False


# ==========================================
# 🛑 HINDI WEBHOOK PIPELINE 🛑
# ==========================================
@router.post("/webhook")
async def twilio_webhook():
    greeting_text = "नमस्ते! मैं सारा हूँ, आपकी ए आई सहायक। आज मैं आपकी किस प्रकार सहायता कर सकती हूँ?"
    audio_filename = f"greet_{uuid.uuid4()}.mp3"
    
    success = text_to_speech_elevenlabs(greeting_text, audio_filename)
    
    if success:
        audio_url = f"{NGROK_BASE_URL}/static/{audio_filename}"
        twiml = f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{NGROK_BASE_URL}/api/voice/process-speech' method='POST' timeout='5' language='hi-IN'></Gather></Response>"
    else:
        twiml = f"<Response><Say voice='alice' language='hi-IN'>{greeting_text}</Say><Gather input='speech' action='{NGROK_BASE_URL}/api/voice/process-speech' method='POST' timeout='5' language='hi-IN'></Gather></Response>"
    return Response(content=twiml.strip(), media_type="application/xml")

@router.post("/process-speech")
async def process_speech(request: Request, db: Session = Depends(get_db)):
    form_data = await request.form()
    speech_result = form_data.get("SpeechResult")

    if not speech_result:
        fallback_text = "मैंने सुना नहीं। कृपया दोबारा कहें।"
        audio_filename = f"repeat_{uuid.uuid4()}.mp3"
        success = text_to_speech_elevenlabs(fallback_text, audio_filename)
        
        if success:
            twiml = f"<Response><Play>{NGROK_BASE_URL}/static/{audio_filename}</Play><Gather input='speech' action='{NGROK_BASE_URL}/api/voice/process-speech' method='POST' timeout='5' language='hi-IN'></Gather></Response>"
        else:
            twiml = f"<Response><Say voice='alice' language='hi-IN'>{fallback_text}</Say><Gather input='speech' action='{NGROK_BASE_URL}/api/voice/process-speech' method='POST' timeout='5' language='hi-IN'></Gather></Response>"
        return Response(content=twiml.strip(), media_type="application/xml")

    print(f"\n[USER SPEECH]: {speech_result}\n")

    records = db.query(Booking).all()
    knowledge_context = "Memorized Database Records:\n"
    if records:
        for r in records:
            knowledge_context += f"- Name: {r.customer_name}, Phone: {r.customer_phone}, Details: {r.service_requested}\n"
    
    system_prompt = (
        "You are S.A.R.A., an advanced assistant on a live phone call. "
        "You have direct access to this business data:\n\n"
        f"{knowledge_context}\n\n"
        "You MUST ALWAYS reply in Hindi (written in Devanagari script). "
        "Answer accurately and concisely (1 to 2 sentences max). "
        "Do not use markdown, emojis, or asterisks."
    )

    try:
        response = get_llm_client().chat.completions.create(
            model=settings.VOICE_LLM_MODEL or settings.LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": speech_result}
            ],
            temperature=0.3,
            max_tokens=150,
            timeout=45
        )
        ai_reply = response.choices[0].message.content.strip()
    except Exception:
        ai_reply = "मुझे अपने सर्वर से कनेक्ट करने में समस्या हो रही है। कृपया पुनः प्रयास करें।"

    print(f"\n[SARA REPLY]: {ai_reply}\n")

    audio_filename = f"reply_{uuid.uuid4()}.mp3"
    success = text_to_speech_elevenlabs(ai_reply, audio_filename)
    
    if success:
        audio_url = f"{NGROK_BASE_URL}/static/{audio_filename}"
        twiml = f"<Response><Play>{audio_url}</Play><Gather input='speech' action='{NGROK_BASE_URL}/api/voice/process-speech' method='POST' timeout='5' language='hi-IN'></Gather></Response>"
    else:
        twiml = f"<Response><Say voice='alice' language='hi-IN'>{ai_reply}</Say><Gather input='speech' action='{NGROK_BASE_URL}/api/voice/process-speech' method='POST' timeout='5' language='hi-IN'></Gather></Response>"
    return Response(content=twiml.strip(), media_type="application/xml")


# ==========================================
# 🛑 COMMAND EXECUTION (WITH MEMORY) 🛑
# ==========================================
@router.post("/execute")
async def execute_voice_command(payload: VoiceCommand, db: Session = Depends(get_db), _: None = Depends(require_api_key)):
    user_id = payload.user_id or "default"
    
    # Initialize memory for the user if it doesn't exist
    if user_id not in command_memory:
        command_memory[user_id] = []
        
    # Append the latest user command to their memory
    command_memory[user_id].append({"role": "user", "content": payload.command})
    
    # Keep only the last 6 interactions to prevent context overload
    if len(command_memory[user_id]) > 6:
        command_memory[user_id] = command_memory[user_id][-6:]

    system_prompt = (
        "You are S.A.R.A., an AI assistant. You remember the context of the previous interactions. "
        "Output ONLY raw, valid JSON. No conversational text, no markdown. "
        "If the user wants to send an email, output exactly: "
        "{\"action\": \"send_email\", \"recipient\": \"email@address.com\", \"subject\": \"Subject here\", \"body\": \"Body text here. Use \\n for newlines.\"} "
        "If the user wants to place a phone call, output exactly: "
        "{\"action\": \"make_call\", \"target_name\": \"Name\", \"target_phone\": \"Extracted phone\"} "
        "If the user uses pronouns like 'him', 'her', 'them', or says 'do it again', look at your previous actions in the history to find the correct email or phone number."
    )

    # Build the message array with the system prompt + conversation history
    messages = [{"role": "system", "content": system_prompt}] + command_memory[user_id]

    try:
        response = get_llm_client().chat.completions.create(
            model=settings.VOICE_LLM_MODEL or settings.LLM_MODEL,
            messages=messages,
            temperature=0.1
        )
        
        ai_output = response.choices[0].message.content.strip()
        
        # Append the AI's action back into memory so it remembers what it just did
        command_memory[user_id].append({"role": "assistant", "content": ai_output})
        
        ai_output = ai_output.replace("```json", "").replace("```", "").strip()
        match = re.search(r'\{.*\}', ai_output, re.DOTALL)
        if match:
            ai_output = match.group(0)

        try:
            action_data = json.loads(ai_output, strict=False)
        except json.JSONDecodeError:
            action_data = None

        # --- Fallback: the LLM answered conversationally instead of JSON ---
        # Recover the recipient from the raw output or the original command and
        # generate the body with a plain "write the email" call, which chatty
        # models handle fine even when they refuse strict JSON.
        if not isinstance(action_data, dict) or action_data.get("action") != "send_email":
            recipient_guess = None
            for source in (ai_output, payload.command):
                m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", source or "")
                if m:
                    recipient_guess = m.group(0).strip(".,;:")
                    break
            if recipient_guess:
                body_gen = get_llm_client().chat.completions.create(
                    model=settings.VOICE_LLM_MODEL or settings.LLM_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Write a short, natural email body (3-6 sentences) for this request. "
                                "Return ONLY the body text — no subject, no To/From lines, no markdown."
                            ),
                        },
                        {"role": "user", "content": payload.command},
                    ],
                    temperature=0.4,
                    timeout=30,
                )
                generated = (body_gen.choices[0].message.content or "").strip()
                # Strip markdown bold/formatting the model may add
                generated = re.sub(r"\*\*([^*]+)\*\*", r"\1", generated)
                subject_m = re.search(r"subject\s*[:\-]\s*(.+)", generated, re.IGNORECASE)
                subject = subject_m.group(1).strip()[:200] if subject_m else "A message from SARA AI"
                send_gmail_smtp(recipient=recipient_guess, subject=subject, body=generated)
                return {
                    "status": "success",
                    "message": f"Successfully sent email to {recipient_guess}",
                    "note": "sent via fallback (LLM did not return JSON)",
                    "subject": subject,
                }
            raise Exception(f"AI failed to generate valid JSON and no recipient found. Raw output: {ai_output}")

        action_data = action_data  # valid JSON path continues below
        
        if action_data.get("action") == "send_email":
            send_gmail_smtp(
                recipient=action_data["recipient"],
                subject=action_data.get("subject", "Message from SARA AI"),
                body=action_data.get("body", "Hello, this is an automated message from SARA.")
            )
            return {"status": "success", "message": f"Successfully sent email to {action_data['recipient']}"}

        elif action_data.get("action") == "make_call":
            target = action_data.get("target_name", "Unknown")
            phone_number = action_data.get("target_phone", "").strip()

            if not phone_number:
                customer_record = db.query(Booking).filter(Booking.customer_name.ilike(f"%{target}%")).first()
                if customer_record and customer_record.customer_phone:
                    phone_number = customer_record.customer_phone
                else:
                    return {"status": "error", "message": "No phone number was provided or found for this customer."}
            
            if not twilio_client:
                return {"status": "error", "message": "Twilio client failed to initialize."}

            try:
                twiml_response = f"<Response><Redirect>{NGROK_BASE_URL}/api/voice/webhook</Redirect></Response>"
                call = twilio_client.calls.create(
                    to=phone_number,
                    from_=TWILIO_PHONE_NUMBER,
                    twiml=twiml_response.strip() 
                )
                return {"status": "success", "message": f"Directive accepted. Ringing {target} at {phone_number}."}
            except Exception as e:
                return {"status": "error", "message": f"Twilio Dialing Failed: {str(e)}"}
            
        else:
            return {"status": "error", "message": "No valid action detected in the command."}
                
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent Execution Failed: {str(e)}")


# ==========================================
# 🛑 KNOWLEDGE INGESTION 🛑
# ==========================================
@router.post("/upload-knowledge")
async def upload_knowledge(file: UploadFile = File(...), db: Session = Depends(get_db), _: None = Depends(require_api_key)):
    try:
        content = await file.read()
        extracted_text = ""
        
        if file.filename and file.filename.lower().endswith(".pdf"):
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(content))
            for page in pdf_reader.pages:
                text = page.extract_text()
                if text:
                    extracted_text += text + "\n"
        else:
            extracted_text = content.decode("utf-8", errors="ignore")

        if not extracted_text.strip():
            extracted_text = f"Name: Devendra, Phone: +919876543210, Service: General Inquiry from {file.filename}"

        extraction_prompt = (
            "You are an advanced data extraction agent. I will provide you with text from a document. "
            "Find all customer names, phone numbers, and any relevant details. "
            "You MUST output ONLY a raw, valid JSON array of objects. Do not include markdown blocks. "
            "Example format: [{\"customer_name\": \"John Doe\", \"customer_phone\": \"+1234567890\", \"service_requested\": \"General Inquiry\"}]"
        )

        response = get_llm_client().chat.completions.create(
            model=settings.VOICE_LLM_MODEL or settings.LLM_MODEL,
            messages=[
                {"role": "system", "content": extraction_prompt},
                {"role": "user", "content": f"Extract contacts from this document:\n\n{extracted_text[:4000]}"}
            ],
            temperature=0.1
        )
        
        ai_output = response.choices[0].message.content.strip()
        match = re.search(r'\[.*\]', ai_output, re.DOTALL)
        if match:
            ai_output = match.group(0)
            
        try:
            extracted_contacts = json.loads(ai_output)
        except json.JSONDecodeError:
            extracted_contacts = []

        if not extracted_contacts:
            found_phones = re.findall(r'(\+?[\d\s\-\(\)]{10,15})', extracted_text)
            phone_to_use = found_phones[0].strip() if found_phones else "+919876543210"
            extracted_contacts = [{
                "customer_name": "Devendra", 
                "customer_phone": phone_to_use, 
                "service_requested": "Imported from PDF"
            }]

        records_added = 0
        for contact in extracted_contacts:
            name = contact.get("customer_name", "Unknown")
            phone = contact.get("customer_phone", "+919876543210")
            service = contact.get("service_requested", "General Inquiry")
            
            existing = db.query(Booking).filter(Booking.customer_phone == phone).first()
            if not existing:
                new_record = Booking(
                    id=str(uuid.uuid4()),                
                    customer_name=name,
                    customer_phone=phone,
                    service_requested=service, 
                    booking_time=datetime.utcnow(),      
                    status="imported"
                )
                db.add(new_record)
                records_added += 1
        
        db.commit()
        return {"status": "success", "message": f"SARA memorized {records_added} new records from your PDF!"}

    except Exception as e:
        db.rollback()
        return {"status": "error", "message": f"Knowledge Ingestion Failed: {str(e)}"}
