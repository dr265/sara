from pydantic import BaseModel
from datetime import datetime
import uuid

# Schema for incoming data (when frontend sends new user data)
class UserCreate(BaseModel):
    id: str      # The unique ID from your auth provider (e.g., Clerk or Firebase)
    email: str
    name: str

# Schema for outgoing data (when the backend sends user data back)
class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    created_at: datetime

    class Config:
        from_attributes = True # Tells Pydantic to read data directly from SQLAlchemy models

# Schema for returning document data (we exclude the massive vector array to keep it fast)
class DocumentResponse(BaseModel):
    id: str  # Document.id is a String column, not a UUID
    user_id: str
    filename: str
    content: str
    created_at: datetime

    class Config:
        from_attributes = True