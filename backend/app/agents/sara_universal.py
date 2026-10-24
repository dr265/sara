import os
import logging
from twilio.rest import Client
from datetime import datetime
import uuid
import traceback
from dotenv import load_dotenv

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import your database models
from app.database.models import BusinessProfile, BusinessDocument, Booking

from videosdk.agents import (
    Agent,
    AgentSession,
    Pipeline,
    JobContext,
    RoomOptions,
    WorkerJob,
    Options,
    function_tool,
)
from videosdk.agents._plugins import (
    GeminiRealtime,
    GeminiLiveConfig,
)

logging.basicConfig(level=logging.INFO)
load_dotenv()

# --- Twilio Client Setup ---
twilio_client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
TWILIO_SMS = os.getenv("TWILIO_PHONE_NUMBER")
TWILIO_WA = os.getenv("TWILIO_WHATSAPP_NUMBER")

# --- Database Connection Setup for the Worker ---
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://sara_admin:sara_password@localhost:5432/sara_db")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def fetch_business_context(business_id: str) -> str:
    """Dynamically fetches the business profile and uploaded documents from the DB."""
    db = SessionLocal()
    try:
        profile = db.query(BusinessProfile).filter(BusinessProfile.id == business_id).first()
        if not profile:
            return "Generic SARA Assistant Mode."

        documents = db.query(BusinessDocument).filter(BusinessDocument.business_id == business_id).all()
        doc_text = "\n\n".join([doc.extracted_text for doc in documents if doc.extracted_text])

        context = f"""
        Business Name: {profile.business_name}
        Business Type: {profile.business_type}
        
        COMPANY POLICIES & RULES:
        {profile.rules_and_policies}
        
        BUSINESS DATA (Pricing, Inventory, Services, Customers):
        {doc_text}
        """
        return context
    finally:
        db.close()

# --- SARA'S BRAIN & TOOLS ---

class UniversalSaraAgent(Agent):
    def __init__(self, business_id: str):
        # 1. Fetch dynamic data before initializing the prompt
        self.business_id = business_id
        business_context = fetch_business_context(business_id)

        super().__init__(
            instructions=f"""
You are SARA, an advanced, highly intelligent AI Voice Agent.
You are currently representing the following business:

{business_context}

IMPORTANT RULES:
- Your name is SARA.
- Speak very naturally in a mix of Hindi and English (Hinglish).
- Always be polite, professional, and concise. Do not give long monologues.
- Use the provided BUSINESS DATA to answer customer queries accurately.
- If a customer wants to book an appointment or service, ALWAYS use the 'check_availability' tool first.
- If they confirm the available slot, use the 'create_booking' tool.
- After a successful booking, explicitly tell them: "Aapki booking confirm ho gayi hai, main aapko WhatsApp aur SMS par detail bhej rahi hu."

START OF EVERY CALL:
Always begin exactly with: "Hello! Main SARA baat kar rahi hu {business_context.split('Business Name:')[1].split('Business Type:')[0].strip()} se. Aaj main aapki kaise madad kar sakti hu?"
""",
            tools=[self.check_availability, self.create_booking]
        )

    # --- ACTION TOOLS ---
    
    @function_tool(description="Check available slots for a specific date (Format: YYYY-MM-DD)")
    async def check_availability(self, date: str) -> str:
        logging.info(f"[TOOL] Checking availability for {date} at {self.business_id}")
        return f"Yes, we have open slots on {date}. Available times are 10:00 AM, 1:00 PM, and 4:00 PM."

    @function_tool(description="Create a booking. Requires customer name, phone (with country code), service, and time.")
    async def create_booking(self, customer_name: str, phone: str, service: str, time: str) -> str:
        logging.info(f"[TOOL] Booking {service} for {customer_name} at {time}")
        db = SessionLocal()
        try:
            booking_id = f"BKG-{str(uuid.uuid4())[:6].upper()}"
            
            # 1. Save to Database
            new_booking = Booking(
                id=booking_id,
                business_id=self.business_id,
                customer_name=customer_name,
                customer_phone=phone,
                service_requested=service,
                booking_time=datetime.now() # Use robust parsing for production
            )
            db.add(new_booking)
            db.commit()
            
            # 2. Draft Confirmation Message
            message_body = (
                f"Hello {customer_name},\n"
                f"Your booking for '{service}' is confirmed!\n"
                f"Booking ID: {booking_id}\n"
                f"Time: {time}\n\n"
                f"To cancel, reply 'CANCEL {booking_id}'."
            )

            # 3. Dispatch SMS
            try:
                twilio_client.messages.create(
                    body=message_body,
                    from_=TWILIO_SMS,
                    to=phone
                )
                logging.info(f"[SMS] Successfully sent to {phone}")
            except Exception as e:
                logging.error(f"[SMS ERROR] Failed to send SMS: {str(e)}")

            # 4. Dispatch WhatsApp
            try:
                wa_to = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
                twilio_client.messages.create(
                    body=message_body,
                    from_=TWILIO_WA,
                    to=wa_to
                )
                logging.info(f"[WHATSAPP] Successfully sent to {wa_to}")
            except Exception as e:
                logging.error(f"[WA ERROR] Failed to send WhatsApp: {str(e)}")
            
            return f"Booking successfully created in the database. ID is {booking_id}. Confirmations sent."
            
        except Exception as e:
            db.rollback()
            logging.error(f"[DB ERROR] Failed to create booking: {str(e)}")
            return "Error: Could not save the booking to the database."
        finally:
            db.close()

    # --- LIFECYCLE EVENTS ---
    async def on_enter(self):
        pass

    async def on_exit(self):
        await self.session.say("Call karne ke liye shukriya. Have a great day!")


async def start_session(context: JobContext):
    business_id = context.room_options.metadata.get("business_id", "biz_default")
    agent = UniversalSaraAgent(business_id=business_id)

    model = GeminiRealtime(
        model="gemini-2.0-flash-live-001", 
        api_key=os.getenv("GOOGLE_API_KEY"),
        config=GeminiLiveConfig(
            voice="Aoede", 
            response_modalities=["AUDIO"],
        ),
    )

    pipeline = Pipeline(llm=model)
    session = AgentSession(agent=agent, pipeline=pipeline)

    await session.start(wait_for_participant=True, run_until_shutdown=True)

def make_context():
    return JobContext(room_options=RoomOptions())

if __name__ == "__main__":
    try:
        options = Options(
            agent_id="UniversalSaraAgent",
            register=True,
            max_processes=10,
            host="localhost",
            port=8081,
        )
        job = WorkerJob(entrypoint=start_session, jobctx=make_context, options=options)
        logging.info("Starting Universal SARA AI Voice Worker...")
        job.start()
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    except Exception:
        traceback.print_exc()