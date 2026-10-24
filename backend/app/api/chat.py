import asyncio
import uuid
from functools import lru_cache
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.api.deps import require_api_key
from app.database.database import engine, get_db
from app.database.models import Document, User, ChatMessage 
from app.core.llm_router import get_llm_client
from app.core.config import settings

@lru_cache(maxsize=1)
def get_embedding_model():
    """Load the embedding model only when a route actually needs it.

    The import is deliberately inside the function: `sentence_transformers`
    pulls in torch + transformers (~10s), which must not run at backend
    startup for agents that never use embeddings.
    """
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer("all-MiniLM-L6-v2")

def get_llm_client():
    if not settings.LLM_API_KEY:
        raise HTTPException(status_code=503, detail="LLM_API_KEY is not configured.")
    from app.core.llm_router import get_llm_client as _failover_client
    return _failover_client()

router = APIRouter(prefix="/api/chat", tags=["Chat"])

class ChatQuery(BaseModel):
    user_id: str
    chat_id: str
    question: str

@router.get("/history/{chat_id}")
async def get_chat_history(chat_id: str, db: Session = Depends(get_db), _: None = Depends(require_api_key)):
    messages = db.query(ChatMessage).filter(
        ChatMessage.chat_id == chat_id
    ).order_by(ChatMessage.created_at.asc()).all()
    
    return [{"id": msg.id, "role": msg.role, "content": msg.content} for msg in messages]

@router.post("/ask")
async def ask_question(query: ChatQuery, db: Session = Depends(get_db), _: None = Depends(require_api_key)):
    user = db.query(User).filter(User.id == query.user_id).first()
    if not user:
        try:
            new_user = User(id=query.user_id, email=f"{query.user_id}@placeholder.com", name="Guest User")
            db.add(new_user)
            db.commit()
        except Exception as e:
            db.rollback()
            print("Could not create fallback user:", e)
    
    # sentence-transformers model load + encode are CPU-heavy and block the
    # event loop; run them in a worker thread so concurrent requests stay live.
    question_vector = await asyncio.to_thread(
        lambda: get_embedding_model().encode(query.question).tolist()
    )
    if engine.dialect.name == "postgresql":
        # pgvector: exact cosine distance in SQL
        results = db.query(Document).filter(
            Document.user_id == query.user_id
        ).order_by(
            Document.embedding.cosine_distance(question_vector)
        ).limit(2).all()
    else:
        # SQLite: pgvector operators are unavailable, so score in Python
        import math

        def _cosine(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a)) or 1e-12
            nb = math.sqrt(sum(x * x for x in b)) or 1e-12
            return 1.0 - dot / (na * nb)

        docs = db.query(Document).filter(Document.user_id == query.user_id).all()
        scored = sorted(
            ((_cosine(question_vector, doc.embedding), doc)
             for doc in docs if doc.embedding),
            key=lambda t: t[0],
        )
        results = [doc for _, doc in scored[:2]]
    
    context = "\n\n---\n\n".join([doc.content for doc in results]) if results else "No context uploaded."
    
    history_records = db.query(ChatMessage).filter(
        ChatMessage.chat_id == query.chat_id
    ).order_by(ChatMessage.created_at.desc()).limit(6).all()
    
    history_records.reverse()

    api_messages = [
        {"role": "system", "content": "You are SARA AI, a highly intelligent assistant. Use the provided context and conversation history to answer questions accurately."}
    ]

    for msg in history_records:
        role = "assistant" if msg.role == "ai" else "user"
        api_messages.append({"role": role, "content": msg.content})

    api_messages.append({
        "role": "user", 
        "content": f"Document Context:\n{context}\n\nUser Question: {query.question}"
    })

    try:
        def _call_llm():
            return get_llm_client().chat.completions.create(
                model=settings.CHAT_LLM_MODEL or settings.LLM_MODEL,
                messages=api_messages
            )

        # The OpenAI-compatible HTTP call is synchronous; offload it so the
        # event loop is not blocked for the full LLM round-trip.
        response = await asyncio.to_thread(_call_llm)
        ai_answer = response.choices[0].message.content
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM Error: {str(e)}")

    try:
        user_msg = ChatMessage(user_id=query.user_id, chat_id=query.chat_id, role="user", content=query.question)
        ai_msg = ChatMessage(user_id=query.user_id, chat_id=query.chat_id, role="ai", content=ai_answer)
        db.add(user_msg)
        db.add(ai_msg)
        db.commit()
    except Exception as e:
        db.rollback()
        print("Failed to save to database:", e)
    
    return {"question": query.question, "answer": ai_answer}
