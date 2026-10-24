import os
import json
import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.config import settings
from app.database.database import get_db
from app.database.models import Integration, User

router = APIRouter(prefix="/api/auth", tags=["Auth"])

# Local backup token cache to bypass DB constraints if needed
TOKEN_CACHE_FILE = "gmail_token.json"

class AuthCode(BaseModel):
    code: str
    user_id: str

@router.post("/google")
async def google_auth_callback(payload: AuthCode, db: Session = Depends(get_db)):
    token_url = "https://oauth2.googleapis.com/token"
    
    data = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "code": payload.code,
        "grant_type": "authorization_code",
        "redirect_uri": "http://localhost:3000/auth/callback"
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=data)
        token_data = response.json()

    if "access_token" not in token_data:
        print("GOOGLE TOKEN ERROR:", token_data)
        raise HTTPException(status_code=400, detail=f"Failed to retrieve token from Google: {token_data}")

    # Best-effort: learn which Google account was connected. The sender panel
    # requests the extra "openid email" scopes for this; older clients that
    # only asked for gmail.send simply get nothing back here.
    google_email = ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            info = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            if info.status_code == 200:
                google_email = (info.json() or {}).get("email", "") or ""
    except Exception:
        google_email = ""
    token_data["email"] = google_email

    # 1. Save locally as backup cache to ensure email execution never fails
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump(token_data, f)

    # 2. Save to database
    try:
        # Check if any user already exists
        existing_user = db.query(User).first()
        target_user_id = existing_user.id if existing_user else payload.user_id

        if not existing_user:
            try:
                new_user = User(id=payload.user_id, email=google_email or "dr7409311839@gmail.com")
                db.add(new_user)
                db.commit()
                target_user_id = payload.user_id
            except Exception:
                db.rollback()
        elif google_email and existing_user.email != google_email:
            # Connected a different Google account than last time — remember it
            # so the email Sender panel can show who is connected.
            try:
                existing_user.email = google_email
                db.commit()
            except Exception:
                db.rollback()

        # Update or create the integration record
        existing_integration = db.query(Integration).filter(
            Integration.provider == "gmail"
        ).first()

        if existing_integration:
            existing_integration.access_token = token_data["access_token"]
            if "refresh_token" in token_data:
                existing_integration.refresh_token = token_data["refresh_token"]
        else:
            new_integration = Integration(
                user_id=target_user_id,
                provider="gmail",
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token", "")
            )
            db.add(new_integration)

        db.commit()
    except Exception as e:
        db.rollback()
        print("DB WARNING (using local token cache fallback):", str(e))
    
    return {"status": "success", "email": google_email, "message": "Google account linked securely."}
