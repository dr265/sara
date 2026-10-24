from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from functools import lru_cache
from sqlalchemy.orm import Session
from app.api.deps import require_api_key
from app.database.database import get_db
from app.database.models import Document, User
@lru_cache(maxsize=1)
def get_embedding_model():
    """Avoid downloading/loading a model during API startup.

    Imported lazily on purpose: `sentence_transformers` loads torch +
    transformers (~10s), which must not run when the backend boots.
    """
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer("all-MiniLM-L6-v2")

# FIX 1: Set the correct prefix so the frontend can find this file!
router = APIRouter(tags=["Documents"])

# FIX 2: Create the /upload endpoint expecting a File and user_id Form data
@router.post("/api/documents/upload")
async def upload_document(
    file: UploadFile = File(...), 
    user_id: str = Form(...), 
    db: Session = Depends(get_db),
    _: None = Depends(require_api_key)
):
    # 1. Verify User exists, or auto-create them if they come from Clerk
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        # Create a new user profile in PostgreSQL matching the Clerk ID
        new_user = User(id=user_id, name="Clerk User", email="clerk_" + user_id + "@sara.ai")
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        
    # 2. Read the file content (.txt files)
    try:
        contents = await file.read()
        text_content = contents.decode("utf-8")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read or decode file: {str(e)}")
        
    # 3. Vectorize the document text using SentenceTransformers
    try:
        document_vector = get_embedding_model().encode(text_content).tolist()
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to vectorize document")

    # 4. Save context and embeddings to PostgreSQL
    try:
        new_document = Document(
            user_id=user_id,
            filename=file.filename or "untitled.txt",
            content=text_content,
            embedding=document_vector
        )
        db.add(new_document)
        db.commit()
        db.refresh(new_document)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    return {"message": "Document vectorized and saved successfully!", "filename": file.filename}
