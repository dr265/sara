"""Legacy agent endpoints (chat, orchestrate, WhatsApp) served by the unified SARA backend."""
import os

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

router = APIRouter(tags=["Legacy Agent"])

# --- CONFIGURATION (values live in backend/.env) ---
# All agents share one LLM brain. OpenRouter is retired in favour of the xKiro
# key in LLM_API_KEY (free-tier models); legacy routes reuse the same config.
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = (os.environ.get("LLM_BASE_URL") or "https://api.xkiro.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL") or "deepseek/deepseek-v4-flash"

# WhatsApp Meta API Configuration
# No insecure default: an operator must set a real token in backend/.env.
WHATSAPP_VERIFY_TOKEN = (os.environ.get("WHATSAPP_VERIFY_TOKEN") or "").strip()
WHATSAPP_API_TOKEN = os.environ.get("WHATSAPP_API_TOKEN", "YOUR_META_WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "YOUR_PHONE_NUMBER_ID")


class OrchestratorRequest(BaseModel):
    prompt: str


class ChatRequest(BaseModel):
    agent_id: str
    message: str


def call_llm(system_prompt: str, user_message: str):
    if not LLM_API_KEY:
        raise HTTPException(status_code=503, detail="LLM_API_KEY is not configured in backend/.env")
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]
    }

    response = requests.post(LLM_BASE_URL + "/chat/completions", headers=headers, json=data, timeout=60)

    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"]
    else:
        raise Exception(f"LLM Error: {response.text}")


def send_whatsapp_message(to_number: str, text: str):
    url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_API_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text}
    }
    response = requests.post(url, headers=headers, json=data, timeout=30)
    if response.status_code not in [200, 201]:
        print(f"Failed to send WhatsApp message: {response.text}")


@router.post("/api/v1/orchestrate")
def orchestrate_task(request: OrchestratorRequest):
    """Evaluates user prompt and returns the best Next.js route."""
    try:
        system_prompt = """
        You are the SARA Hologram Orchestrator. Read the user's ultimate goal and route them to the most appropriate agent.
        Available agents and routes:
        - Finance Analyzer (/hub/finance-analyzer)
        - Brand Architect (/hub/brand-architect)
        - BioMed Researcher (/hub/biomed-researcher)
        - Cloud Architect (/hub/cloud-architect)
        - Telegram Booking Assistant (/hub/telegram-assistant)
        - Email Automation Agent (/hub/email-automation)
        - EduLearner AI (/hub/edulearner-ai) — adaptive learning tutor: skill-gap
          analysis, personalised study roadmaps, daily plans, marked practice,
          interview readiness and learner progress reports. Route here whenever the
          user wants to learn, study, upskill, prepare for exams/interviews, or ask
          what to learn next.
        Respond ONLY with the exact route string (e.g., /hub/edulearner-ai) and nothing else. If unsure, default to /hub/general.
        """

        route = call_llm(system_prompt, request.prompt).strip()

        if not route.startswith("/hub/"):
            route = "/hub/general"

        return {
            "status": "success",
            "route": route
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/chat")
def chat_with_agent(request: ChatRequest):
    """Handles dynamic chat for specific sub-agents."""
    try:
        system_prompt = f"You are the {request.agent_id} specialized AI agent within the SARA ecosystem. Provide a helpful, technical, and concise response to the user's prompt based strictly on your specific domain expertise."
        reply = call_llm(system_prompt, request.message)

        return {
            "status": "success",
            "reply": reply
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/whatsapp")
async def verify_whatsapp(request: Request):
    """Required by Meta Cloud API to verify the webhook URL."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN and challenge is not None:
        print("WhatsApp Webhook Verified Successfully!")
        # Meta expects the challenge echoed back as plain text — never an int
        return PlainTextResponse(str(challenge))

    return PlainTextResponse("Verification failed", status_code=403)


@router.post("/api/v1/whatsapp")
async def receive_whatsapp(request: Request):
    """Catches incoming WhatsApp messages and triggers response."""
    try:
        payload = await request.json()

        if "entry" in payload and payload["entry"]:
            entry = payload["entry"][0]
            if "changes" in entry and entry["changes"]:
                change = entry["changes"][0]
                value = change.get("value", {})

                if "messages" in value and value["messages"]:
                    message_obj = value["messages"][0]
                    sender_phone = message_obj["from"]

                    if message_obj["type"] == "text":
                        user_text = message_obj["text"]["body"]

                        system_prompt = "You are SARA, an omnipotent system-control agent now assisting the user over WhatsApp. Provide concise, helpful, and accurately formatted text responses."
                        reply_text = call_llm(system_prompt, user_text)

                        send_whatsapp_message(sender_phone, reply_text)

        return {"status": "success"}

    except Exception as e:
        return {"status": "error", "message": str(e)}