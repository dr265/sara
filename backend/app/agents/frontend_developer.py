import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

DATA_DIR = Path(os.getenv("APP_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data" / "frontend_dev"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATABASE = DATA_DIR / "app.sqlite3"
TOKEN_TTL = 60 * 60 * 24
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
PASSWORD_ITERATIONS = 600_000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend.frontend_dev")
security = HTTPBearer(auto_error=False)
socket_clients: dict[int, set[WebSocket]] = defaultdict(set)


@contextmanager
def database():
    connection = sqlite3.connect(DATABASE, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with database() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                completed INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS tasks_owner ON tasks(user_id, id);

            CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS uploads_owner ON uploads(user_id);
        """)
        connection.execute(
            "DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),)
        )
    yield


app = FastAPI(
    title="Single-File Backend",
    version="1.0.0",
    lifespan=lifespan,
)


class RateLimiter:
    def __init__(self):
        self.buckets: dict[str, deque] = {}
        self.lock = threading.Lock()

    def allow(self, key: str, limit: int, window: int = 60) -> bool:
        now = time.monotonic()
        with self.lock:
            expired = [
                name for name, bucket in self.buckets.items()
                if not bucket or bucket[-1] <= now - window
            ]
            for name in expired:
                del self.buckets[name]

            if key not in self.buckets and len(self.buckets) >= 10_000:
                return False

            bucket = self.buckets.setdefault(key, deque())
            while bucket and bucket[0] <= now - window:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True


limiter = RateLimiter()


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    request_id = secrets.token_hex(8)
    host = request.client.host if request.client else "unknown"
    authentication = request.url.path in ("/auth/register", "/auth/login")
    key = f"{'auth' if authentication else 'api'}:{host}"

    if not limiter.allow(key, 10 if authentication else 120):
        response = JSONResponse(
            {"detail": "Too many requests"}, status_code=429,
            headers={"Retry-After": "60"},
        )
    else:
        response = await call_next(request)

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    logger.error(
        "Unhandled error on %s", request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(InputModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{3,40}", value):
            raise ValueError("Use 3-40 letters, numbers, or underscores")
        return value


class TaskCreate(InputModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Title cannot be blank")
        return value.strip()


class TaskUpdate(TaskCreate):
    completed: bool = False


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PASSWORD_ITERATIONS
    ).hex()
    return f"{salt}:{digest}"


DUMMY_PASSWORD_HASH = hash_password(secrets.token_hex(32))


def check_password(password: str, stored: str) -> bool:
    return hmac.compare_digest(
        hash_password(password, stored.split(":", 1)[0]), stored
    )


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_for_token(token: str):
    with database() as connection:
        row = connection.execute(
            """
            SELECT users.id, users.username, users.created_at
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            (token_hash(token), int(time.time())),
        ).fetchone()
    return dict(row) if row else None


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    user = user_for_token(credentials.credentials) if credentials else None
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Missing, expired, or invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def task_result(row) -> dict:
    result = dict(row)
    result["completed"] = bool(result["completed"])
    return result


async def notify(user_id: int, event: str, data: dict):
    for websocket in tuple(socket_clients.get(user_id, ())):
        try:
            await asyncio.wait_for(
                websocket.send_json({"event": event, "data": data}), timeout=2
            )
        except Exception:
            socket_clients[user_id].discard(websocket)


@app.get("/")
def root():
    return {"name": app.title, "version": app.version, "docs": "/docs"}


@app.get("/health")
def health():
    with database() as connection:
        connection.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(payload: Credentials):
    password = hash_password(payload.password)
    created_at = int(time.time())
    try:
        with database() as connection:
            cursor = connection.execute(
                "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
                (payload.username, password, created_at),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Username is already registered") from None
    return {
        "id": user_id, "username": payload.username, "created_at": created_at
    }


@app.post("/auth/login")
def login(payload: Credentials):
    with database() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE username = ?", (payload.username,)
        ).fetchone()

    valid = check_password(
        payload.password, row["password_hash"] if row else DUMMY_PASSWORD_HASH
    )
    if not row or not valid:
        raise HTTPException(
            401, "Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = secrets.token_urlsafe(48)
    with database() as connection:
        connection.execute(
            "DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),)
        )
        connection.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash(token), row["id"], int(time.time()) + TOKEN_TTL),
        )
    return {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_TTL}


@app.post("/auth/logout", status_code=204)
def logout(
    user=Depends(current_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    with database() as connection:
        connection.execute(
            "DELETE FROM sessions WHERE token_hash = ?",
            (token_hash(credentials.credentials),),
        )
    return Response(status_code=204)


@app.get("/auth/me")
def profile(user=Depends(current_user)):
    return user


@app.get("/tasks")
def list_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user=Depends(current_user),
):
    with database() as connection:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user["id"], limit, offset),
        ).fetchall()
        total = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id = ?", (user["id"],)
        ).fetchone()[0]
    return {
        "items": [task_result(row) for row in rows],
        "total": total, "limit": limit, "offset": offset,
    }


@app.get("/tasks/{task_id}")
def get_task(task_id: int, user=Depends(current_user)):
    with database() as connection:
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Task not found")
    return task_result(row)


def insert_task(payload: TaskCreate, user: dict):
    now = int(time.time())
    with database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO tasks(user_id, title, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user["id"], payload.title, payload.description, now, now),
        )
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return task_result(row)


@app.post("/tasks", status_code=201)
async def create_task(payload: TaskCreate, user=Depends(current_user)):
    result = await run_in_threadpool(insert_task, payload, user)
    await notify(user["id"], "task.created", result)
    return result


def edit_task(task_id: int, payload: TaskUpdate, user: dict):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return get_task(task_id, user)

    changes["updated_at"] = int(time.time())
    assignments = ", ".join(f"{field} = ?" for field in changes)
    with database() as connection:
        cursor = connection.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ? AND user_id = ?",
            (*changes.values(), task_id, user["id"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Task not found")
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return task_result(row)


class TaskPatch(InputModel):
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=10_000)
    completed: bool = False

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Title cannot be blank")
        return value.strip()


@app.patch("/tasks/{task_id}")
async def update_task(
    task_id: int, payload: TaskPatch, user=Depends(current_user)
):
    result = await run_in_threadpool(edit_task, task_id, payload, user)
    await notify(user["id"], "task.updated", result)
    return result


def remove_task(task_id: int, user: dict):
    with database() as connection:
        cursor = connection.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Task not found")


@app.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: int, user=Depends(current_user)):
    await run_in_threadpool(remove_task, task_id, user)
    await notify(user["id"], "task.deleted", {"id": task_id})
    return Response(status_code=204)


@app.post("/uploads", status_code=201)
def upload_file(
    file: UploadFile = File(...), user=Depends(current_user)
):
    file_id = secrets.token_hex(24)
    path = UPLOAD_DIR / file_id
    filename = (file.filename or "download").replace("\\", "/").split("/")[-1]
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)[:200] or "download"
    size = 0

    try:
        with path.open("xb") as destination:
            while chunk := file.file.read(64 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Maximum upload size is 10 MiB")
                destination.write(chunk)

        with database() as connection:
            connection.execute(
                """
                INSERT INTO uploads(id, user_id, filename, size, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (file_id, user["id"], filename, size, int(time.time())),
            )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        file.file.close()

    return {"id": file_id, "filename": filename, "size": size}


@app.get("/uploads")
def list_uploads(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user=Depends(current_user),
):
    with database() as connection:
        rows = connection.execute(
            """
            SELECT id, filename, size, created_at FROM uploads
            WHERE user_id = ? ORDER BY created_at DESC, id LIMIT ? OFFSET ?
            """,
            (user["id"], limit, offset),
        ).fetchall()
    return {"items": [dict(row) for row in rows], "limit": limit, "offset": offset}


@app.get("/uploads/{file_id}")
def download_file(file_id: str, user=Depends(current_user)):
    with database() as connection:
        row = connection.execute(
            "SELECT * FROM uploads WHERE id = ? AND user_id = ?",
            (file_id, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404, "File not found")
    path = UPLOAD_DIR / row["id"]
    if not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(
        path, filename=row["filename"], media_type="application/octet-stream"
    )


# --- LLM Chat Integration ---
from openai import OpenAI
from app.core.config import settings

def get_llm_client():
    if not settings.LLM_API_KEY:
        raise HTTPException(status_code=503, detail="LLM_API_KEY is not configured.")
    from app.core.llm_router import get_llm_client as _failover_client
    return _failover_client()

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage]

@app.post("/chat")
async def chat_infer(req: ChatRequest):
    # This route allows anonymous testing for the prototype
    client = get_llm_client()
    try:
        api_messages = [
            {
                "role": "system",
                "content": (
                    "You are JungleMind, an elite, award-winning Senior Frontend Designer. "
                    "You MUST generate ultra-premium, modern, and bug-free code. "
                    "CRITICAL DESIGN RULES: "
                    "1. Always use Glassmorphism (backdrop-blur, semi-transparent backgrounds with white/dark borders). "
                    "2. Incorporate 3D CSS effects (transforms, deep drop-shadows, gradients, and floating elements). "
                    "3. Use dynamic micro-animations for hover states and transitions. "
                    "Output strictly as Single-File HTML using TailwindCSS and embedded JS/CSS within the HTML document. "
                    "Wrap your code entirely within a single ```html ... ``` markdown block. Do not use external non-CDN dependencies."
                )
            }
        ]
        api_messages.extend([{"role": msg.role, "content": msg.content} for msg in req.messages])
        
        response = client.chat.completions.create(
            model=settings.FRONTEND_LLM_MODEL or settings.LLM_MODEL,
            messages=api_messages
        )
        return {"answer": response.choices[0].message.content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM Error: {str(e)}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    host = websocket.client.host if websocket.client else "unknown"
    if not limiter.allow(f"ws:{host}", 20):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    user = None
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        token = message.get("token") if isinstance(message, dict) else None
        if not isinstance(token, str) or len(token) > 256:
            await websocket.close(code=1008)
            return

        user = await run_in_threadpool(user_for_token, token)
        if not user or len(socket_clients[user["id"]]) >= 5:
            await websocket.close(code=1008)
            return

        socket_clients[user["id"]].add(websocket)
        await websocket.send_json({"event": "connected", "user_id": user["id"]})

        while True:
            await asyncio.sleep(15)
            if not await run_in_threadpool(user_for_token, token):
                await websocket.close(code=1008)
                break
            await asyncio.wait_for(
                websocket.send_json({"event": "heartbeat"}), timeout=5
            )
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, RuntimeError):
        try:
            await websocket.close(code=1008)
        except (RuntimeError, OSError):
            pass
    finally:
        if user:
            clients = socket_clients.get(user["id"])
            if clients is not None:
                clients.discard(websocket)
                if not clients:
                    socket_clients.pop(user["id"], None)
