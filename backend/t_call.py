import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv(".env")
from twilio.rest import Client
import os, time
c = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
base = os.environ.get("PUBLIC_BASE_URL","").rstrip("/")
call = c.calls.create(to="+919000000000", from_=os.environ["TWILIO_PHONE_NUMBER"],
    url=f"{base}/voice-agent/voice/twilio/inbound",
    status_callback=f"{base}/voice-agent/voice/twilio/status-callback")
print("PLACED:", call.sid, flush=True)
for i in range(16):
    time.sleep(5)
    cur = c.calls(call.sid).fetch()
    print("  t+%ds %s dur=%s" % ((i+1)*5, cur.status, cur.duration), flush=True)
    if cur.status in ("completed","busy","failed","no-answer","canceled"): break
