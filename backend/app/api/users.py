from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.api.deps import require_api_key
from app.database.database import get_db
from app.database.models import User
from app.api.schemas import UserCreate, UserResponse

# Create a router specifically for user-related endpoints
router = APIRouter(prefix="/api/users", tags=["Users"])

@router.post("/", response_model=UserResponse)
def create_user(user: UserCreate, db: Session = Depends(get_db), _: None = Depends(require_api_key)):
    """Registers a new user in the PostgreSQL database."""
    # Check if user already exists
    existing_user = db.query(User).filter(User.id == user.id).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="User already registered")
    
    # Save the new user to the database
    new_user = User(id=user.id, email=user.email, name=user.name)
    db.add(new_user)
    db.commit()
    db.refresh(new_user) # Get the newly created user (with the created_at timestamp)
    
    return new_user

@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: str, db: Session = Depends(get_db), _: None = Depends(require_api_key)):
    """Fetches a user's details by their ID."""
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return db_user