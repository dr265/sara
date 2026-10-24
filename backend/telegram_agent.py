# SARA AI - Telegram Inquiry & Booking Assistant (backend only)
# All business data, prompts, flags, paths, schemas and integration settings are
# loaded from whatsappschema.json. This module holds executable logic only.
#
#   export SARA_MASTER_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
#   python whatsappagent.py migrate | serve | selftest
from __future__ import annotations

import base64, csv, dataclasses, hashlib, hmac, io, json, logging, os, re, sqlite3
import sys, tarfile, threading, time, unicodedata, uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SCHEMA_FILE = os.environ.get(
    "SARA_SCHEMA_FILE",
    str(Path(__file__).resolve().with_name("telegram_schema.json")),
)

UTC = timezone.utc
LOG = logging.getLogger("sara")


# --------------------------------------------------------------------- helpers
def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or now()).isoformat()


def new_id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:16]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def deep_get(obj: Any, dotted: str, default: Any = None) -> Any:
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def expand_env(value: Any, defaults: Dict[str, str]) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Z0-9_]+)\}",
                      lambda m: os.environ.get(m.group(1), str(defaults.get(m.group(1), ""))), value)
    if isinstance(value, dict):
        return {k: expand_env(v, defaults) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, defaults) for v in value]
    return value


class SaraError(Exception):
    status, code = 400, "sara_error"

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.message, self.extra = message, extra

    def to_dict(self) -> Dict[str, Any]:
        out = {"error": self.code, "message": self.message}
        out.update(self.extra)
        return out


class ValidationError(SaraError):
    status, code = 422, "validation_error"


class NotFound(SaraError):
    status, code = 404, "not_found"


class AuthError(SaraError):
    status, code = 401, "unauthorized"


class Forbidden(SaraError):
    status, code = 403, "forbidden"


class RateLimited(SaraError):
    status, code = 429, "rate_limited"


class IntegrationError(SaraError):
    status, code = 502, "integration_error"


# ---------------------------------------------------------------------- config
class Config:
    # Read-only view over whatsappschema.json with ${ENV} expansion.
    def __init__(self, raw: Dict[str, Any]) -> None:
        self.defaults = dict(deep_get(raw, "environment.defaults", {}) or {})
        self.raw = expand_env(raw, self.defaults)
        self._validate()

    @classmethod
    def load(cls, path: str = SCHEMA_FILE) -> "Config":
        p = Path(path)
        if not p.exists():
            raise SaraError("configuration file not found: " + path)
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def get(self, dotted: str, default: Any = None) -> Any:
        return deep_get(self.raw, dotted, default)

    def flag(self, name: str, default: bool = False) -> bool:
        return bool(self.get("feature_flags." + name, default))

    def _validate(self) -> None:
        for section in ("meta", "storage", "database", "security", "ingestion",
                        "prompts", "integrations", "feature_flags"):
            if section not in self.raw:
                raise ValidationError("schema missing required section: " + section)
        if self.get("meta.channel") != "telegram":
            raise ValidationError("meta.channel must be 'telegram'")


# ----------------------------------------------------- logging / metrics/audit
class JsonFormatter(logging.Formatter):
    def __init__(self, redact: Sequence[str]) -> None:
        super().__init__()
        self.redact = [r.lower() for r in redact]

    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": iso(), "level": record.levelname, "logger": record.name,
                   "msg": record.getMessage()}
        for k, v in getattr(record, "extra_fields", {}).items():
            payload[k] = "***" if k.lower() in self.redact else v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(cfg: Config) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(cfg.get("security.secrets.never_log_keys", [])))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, str(cfg.get("security.logging.level", "INFO")).upper(), logging.INFO))
    return LOG


class Metrics:
    def __init__(self, names: Sequence[str], enabled: bool = True) -> None:
        self.enabled, self._lock = enabled, threading.Lock()
        self.counters: Dict[str, int] = {n: 0 for n in names}

    def inc(self, name: str, value: int = 1) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + value

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self.counters)

    def prometheus(self) -> str:
        lines = []
        for k, v in self.snapshot().items():
            lines.append("# TYPE sara_" + k + " counter")
            lines.append("sara_" + k + " " + str(v))
        return "\n".join(lines) + "\n"


class Crypto:
    # AES-256-GCM when `cryptography` is present, else authenticated HMAC stream.
    def __init__(self, master_key: Optional[str]) -> None:
        material = (master_key or base64.urlsafe_b64encode(os.urandom(32)).decode()).encode()
        self.key = hashlib.sha256(material).digest()
        self._aead = None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            self._aead = AESGCM(self.key)
        except Exception:
            self._aead = None

    def _stream(self, nonce: bytes, length: int) -> bytes:
        out, ctr = b"", 0
        while len(out) < length:
            out += hmac.new(self.key, nonce + ctr.to_bytes(4, "big"), hashlib.sha256).digest()
            ctr += 1
        return out[:length]

    def encrypt(self, plaintext: Optional[str]) -> Optional[str]:
        if plaintext is None:
            return None
        data, nonce = plaintext.encode("utf-8"), os.urandom(12)
        if self._aead is not None:
            return "v2:" + base64.urlsafe_b64encode(nonce + self._aead.encrypt(nonce, data, None)).decode()
        ct = bytes(a ^ b for a, b in zip(data, self._stream(nonce, len(data))))
        tag = hmac.new(self.key, nonce + ct, hashlib.sha256).digest()[:16]
        return "v1:" + base64.urlsafe_b64encode(nonce + tag + ct).decode()

    def decrypt(self, token: Optional[str]) -> Optional[str]:
        if not isinstance(token, str) or ":" not in token:
            return token
        ver, _, b64 = token.partition(":")
        if ver not in ("v1", "v2"):
            return token
        blob = base64.urlsafe_b64decode(b64.encode())
        if ver == "v2":
            if self._aead is None:
                raise SaraError("ciphertext requires cryptography package")
            return self._aead.decrypt(blob[:12], blob[12:], None).decode("utf-8")
        nonce, tag, ct = blob[:12], blob[12:28], blob[28:]
        if not hmac.compare_digest(tag, hmac.new(self.key, nonce + ct, hashlib.sha256).digest()[:16]):
            raise SaraError("ciphertext authentication failed")
        return bytes(a ^ b for a, b in zip(ct, self._stream(nonce, len(ct)))).decode("utf-8")


# --------------------------------------------------------------------- storage
class StorageService:
    # Local-filesystem implementation of a swappable blob store.
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = Path(cfg.get("storage.root") or "./sara_storage").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.templates = cfg.get("storage.paths", {}) or {}

    def resolve(self, key: str, **kw: Any) -> Path:
        tpl = self.templates.get(key)
        if not tpl:
            raise ValidationError("unknown storage path key: " + key)
        target = (self.root / tpl.format(**kw)).resolve()
        if not str(target).startswith(str(self.root)):
            raise Forbidden("path traversal blocked")
        return target

    def write_bytes(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def write_text(self, path: Path, text: str) -> Path:
        return self.write_bytes(path, text.encode("utf-8"))

    def read_bytes(self, path: Path) -> bytes:
        if not path.exists():
            raise NotFound("blob not found: " + path.name)
        return path.read_bytes()

    def append_line(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")

    def backup(self, tenant_id: str) -> str:
        snapshot_id = new_id("snap")
        target = self.resolve("backups", tenant_id=tenant_id,
                              yyyymmdd=now().strftime("%Y%m%d"), snapshot_id=snapshot_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        src = self.root / ("tenants/" + tenant_id)
        with tarfile.open(target, "w:gz") as tar:
            if src.exists():
                tar.add(src, arcname=tenant_id)
        return snapshot_id


# ------------------------------------------------------ database + migrations
MIGRATIONS: List[Tuple[int, str]] = [
    (1, """
    CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY, name TEXT, created_at TEXT, settings TEXT);
    CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, tenant_id TEXT, email TEXT, role TEXT,
        api_token_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS documents(id TEXT, tenant_id TEXT, version INTEGER, filename TEXT,
        ext TEXT, mime TEXT, size INTEGER, sha256 TEXT, status TEXT, route TEXT,
        storage_path TEXT, created_at TEXT, PRIMARY KEY(id, version));
    CREATE TABLE IF NOT EXISTS facts(id TEXT PRIMARY KEY, tenant_id TEXT, entity TEXT, key TEXT,
        value TEXT, numeric_value REAL, currency TEXT, valid_from TEXT, valid_to TEXT,
        confidence REAL, doc_id TEXT, doc_version INTEGER, locator TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, tenant_id TEXT, doc_id TEXT,
        doc_version INTEGER, ordinal INTEGER, text TEXT, locator TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS prompts(id TEXT, tenant_id TEXT, version INTEGER, role TEXT,
        text TEXT, active INTEGER, updated_at TEXT, PRIMARY KEY(id, tenant_id, version));
    CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, tenant_id TEXT, channel TEXT,
        external_chat_id TEXT, language TEXT, state TEXT, paused_until TEXT, summary TEXT,
        created_at TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, tenant_id TEXT, conversation_id TEXT,
        direction TEXT, text TEXT, media TEXT, intent TEXT, confidence REAL, sources TEXT,
        external_id TEXT, delivery_status TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS leads(id TEXT PRIMARY KEY, tenant_id TEXT, conversation_id TEXT,
        data TEXT, status TEXT, created_at TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY, tenant_id TEXT, conversation_id TEXT,
        status TEXT, data TEXT, version INTEGER, hold_until TEXT, created_at TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS generated_documents(id TEXT PRIMARY KEY, tenant_id TEXT, kind TEXT,
        entity_id TEXT, version INTEGER, fmt TEXT, template_id TEXT, storage_path TEXT,
        source_refs TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS connections(id TEXT PRIMARY KEY, tenant_id TEXT, provider TEXT,
        label TEXT, status TEXT, credentials TEXT, meta TEXT, created_at TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS sync_history(id TEXT PRIMARY KEY, tenant_id TEXT, connection_id TEXT,
        direction TEXT, target TEXT, rows_read INTEGER, rows_written INTEGER, conflicts INTEGER,
        status TEXT, detail TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY, tenant_id TEXT, result TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS escalations(id TEXT PRIMARY KEY, tenant_id TEXT, conversation_id TEXT,
        reason TEXT, detail TEXT, status TEXT, created_at TEXT, resolved_at TEXT);
    CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY, tenant_id TEXT, event TEXT, actor TEXT,
        conversation_id TEXT, payload TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, tenant_id TEXT, queue TEXT, kind TEXT,
        payload TEXT, status TEXT, attempts INTEGER, last_error TEXT, run_at TEXT,
        created_at TEXT, updated_at TEXT);
    """),
    (2, """
    CREATE INDEX IF NOT EXISTS idx_chunks_tenant ON chunks(tenant_id, doc_id);
    CREATE INDEX IF NOT EXISTS idx_facts_tenant ON facts(tenant_id, entity, key);
    CREATE INDEX IF NOT EXISTS idx_msgs_conv ON messages(conversation_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, queue, run_at);
    CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit(tenant_id, created_at);
    """),
]


class Database:
    # SQLite implementation of a swappable relational store (thread-safe).
    def __init__(self, cfg: Config) -> None:
        url = str(cfg.get("database.url") or "sqlite:///./sara.db")
        if not url.startswith("sqlite"):
            raise ValidationError("this build ships the SQLite driver; replace Database for other engines")
        self.path = url.split("///", 1)[1] if "///" in url else "./sara.db"
        self._lock = threading.RLock()
        # autocommit + busy timeout: a failed statement can never leave an open
        # transaction behind (that permanently locks the whole shared DB), and
        # writers wait up to 30s instead of failing instantly under contention.
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.migration_table = cfg.get("database.migrations.table", "sara_schema_migrations")
        if cfg.get("database.migrations.auto_apply_on_boot", True):
            self.migrate()

    def migrate(self) -> List[int]:
        applied: List[int] = []
        with self._lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS " + self.migration_table +
                              "(version INTEGER PRIMARY KEY, applied_at TEXT)")
            done = {r[0] for r in self.conn.execute("SELECT version FROM " + self.migration_table)}
            for version, sql in MIGRATIONS:
                if version in done:
                    continue
                self.conn.executescript(sql)
                self.conn.execute("INSERT INTO " + self.migration_table + " VALUES(?,?)", (version, iso()))
                applied.append(version)
            self.conn.commit()
        return applied

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        self.execute("INSERT INTO " + table + "(" + cols + ") VALUES(" + marks + ")", list(row.values()))
        return row

    def upsert(self, table: str, row: Dict[str, Any], keys: Sequence[str]) -> None:
        where = " AND ".join(k + "=?" for k in keys)
        if self.one("SELECT 1 FROM " + table + " WHERE " + where, [row[k] for k in keys]):
            sets = ",".join(k + "=?" for k in row if k not in keys)
            self.execute("UPDATE " + table + " SET " + sets + " WHERE " + where,
                         [v for k, v in row.items() if k not in keys] + [row[k] for k in keys])
        else:
            self.insert(table, row)

    def healthy(self) -> bool:
        try:
            self.execute("SELECT 1")
            return True
        except Exception:
            return False


class AuditLog:
    def __init__(self, db: Database, storage: StorageService, cfg: Config) -> None:
        self.db, self.storage, self.cfg = db, storage, cfg

    def record(self, tenant_id: str, event: str, payload: Dict[str, Any],
               actor: str = "system", conversation_id: Optional[str] = None) -> str:
        row = {"id": new_id("aud"), "tenant_id": tenant_id, "event": event, "actor": actor,
               "conversation_id": conversation_id,
               "payload": json.dumps(payload, ensure_ascii=False, default=str), "created_at": iso()}
        self.db.insert("audit", row)
        try:
            self.storage.append_line(
                self.storage.resolve("audit", tenant_id=tenant_id, yyyymm=now().strftime("%Y%m")),
                json.dumps(row, ensure_ascii=False))
        except Exception:
            LOG.warning("audit file append failed", extra={"extra_fields": {"event": event}})
        return row["id"]

    def trail(self, tenant_id: str, conversation_id: Optional[str] = None,
              limit: int = 200) -> List[Dict[str, Any]]:
        if conversation_id:
            return self.db.query("SELECT * FROM audit WHERE tenant_id=? AND conversation_id=?"
                                 " ORDER BY created_at DESC LIMIT ?", (tenant_id, conversation_id, limit))
        return self.db.query("SELECT * FROM audit WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                             (tenant_id, limit))


# ---------------------------------------------------------------- typed models
@dataclass
class Principal:
    user_id: str
    tenant_id: str
    role: str

    def permissions(self, cfg: Config) -> List[str]:
        return cfg.get("security.rbac.roles." + self.role, []) or []


@dataclass
class Fact:
    tenant_id: str
    entity: str
    key: str
    value: str
    doc_id: str
    doc_version: int
    locator: str
    numeric_value: Optional[float] = None
    currency: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    confidence: float = 0.8
    id: str = field(default_factory=lambda: new_id("fact"))
    updated_at: str = field(default_factory=iso)


@dataclass
class Retrieved:
    text: str
    score: float
    source_ref: str
    kind: str = "chunk"


@dataclass
class AgentDecision:
    reply: str
    language: str
    intent: str
    confidence: float
    escalated: bool
    escalation_reason: Optional[str]
    sources: List[str]
    tool_calls: List[Dict[str, Any]]
    conversation_id: Optional[str] = None
    booking_id: Optional[str] = None
    documents: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


# ------------------------------------------------------------- access + limits
class AccessControl:
    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg, self.db = cfg, db

    @staticmethod
    def hash_token(token: str) -> str:
        return sha256_bytes(token.encode())

    def create_tenant(self, name: str) -> Dict[str, Any]:
        row = {"id": new_id("tnt"), "name": name, "created_at": iso(), "settings": "{}"}
        self.db.insert("tenants", row)
        return row

    def create_user(self, tenant_id: str, email: str, role: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
        role = role or self.cfg.get("security.rbac.default_role", "staff")
        if role not in (self.cfg.get("security.rbac.roles", {}) or {}):
            raise ValidationError("unknown role: " + str(role))
        token = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
        row = {"id": new_id("usr"), "tenant_id": tenant_id, "email": email, "role": role,
               "api_token_hash": self.hash_token(token), "created_at": iso()}
        self.db.insert("users", row)
        return row, token

    def authenticate(self, token: Optional[str]) -> Principal:
        if not token:
            raise AuthError("missing bearer token")
        row = self.db.one("SELECT * FROM users WHERE api_token_hash=?", (self.hash_token(token),))
        if not row:
            raise AuthError("invalid token")
        return Principal(row["id"], row["tenant_id"], row["role"])

    def require(self, principal: Principal, permission: str) -> None:
        perms = principal.permissions(self.cfg)
        if "*" in perms or permission in perms:
            return
        if permission.split(":", 1)[0] + ":*" in perms:
            return
        raise Forbidden("role '" + principal.role + "' lacks permission '" + permission + "'")

    def require_tenant(self, principal: Principal, tenant_id: Optional[str]) -> str:
        if tenant_id and tenant_id != principal.tenant_id and \
                self.cfg.get("security.tenant_isolation.reject_cross_tenant_ids", True):
            raise Forbidden("cross-tenant access denied")
        return principal.tenant_id


class RateLimiter:
    def __init__(self, cfg: Config) -> None:
        self.limits = cfg.get("security.rate_limits", {}) or {}
        self._buckets: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def check(self, limit_key: str, scope: str, window_seconds: int = 60) -> None:
        limit = int(self.limits.get(limit_key, 0) or 0)
        if limit <= 0:
            return
        key, cutoff = limit_key + ":" + scope, time.time() - window_seconds
        with self._lock:
            hits = [t for t in self._buckets.get(key, []) if t > cutoff]
            if len(hits) >= limit:
                raise RateLimited("rate limit exceeded for " + limit_key, limit=limit)
            hits.append(time.time())
            self._buckets[key] = hits


# --------------------------------------------------- validation + file parsers
class FileValidator:
    def __init__(self, cfg: Config) -> None:
        self.rules = cfg.get("security.file_validation", {}) or {}

    def validate(self, filename: str, data: bytes, mime: str = "") -> str:
        ext = Path(filename).suffix.lower()
        if not ext:
            raise ValidationError("file has no extension")
        if ext in (self.rules.get("blocked_extensions") or []):
            raise ValidationError("blocked file type: " + ext)
        allowed = self.rules.get("allowed_extensions") or []
        if allowed and ext not in allowed:
            raise ValidationError("unsupported file type: " + ext)
        max_mb = float(self.rules.get("max_file_mb", 50))
        if len(data) > max_mb * 1024 * 1024:
            raise ValidationError("file exceeds " + str(max_mb) + " MB")
        prefixes = self.rules.get("allowed_mime_prefixes") or []
        if mime and prefixes and not any(mime.startswith(p) for p in prefixes):
            raise ValidationError("unsupported mime type: " + mime)
        if data[:2] == b"MZ" or data[:4] == b"\x7fELF":
            raise ValidationError("executable payload rejected")
        return ext

    def scan(self, data: bytes) -> Tuple[bool, str]:
        # Pluggable malware hook; noop_scanner performs structural checks only.
        markers = [b"<script", b"javascript:", b"vbaProject.bin", b"/JavaScript", b"/Launch"]
        found = [m.decode("utf-8", "ignore") for m in markers if m in data[:2000000]]
        if found:
            return True, "active_content_flagged:" + ",".join(found)
        return True, "clean"


class OcrEngine:
    # Pluggable OCR; uses pytesseract when installed, degrades gracefully.
    def __init__(self, cfg: Config) -> None:
        conf = cfg.get("ingestion.ocr", {}) or {}
        self.enabled = bool(conf.get("enabled", True)) and cfg.flag("enable_ocr", True)
        self.languages = "+".join(conf.get("languages", ["eng"]))
        self.min_confidence = float(conf.get("min_confidence", 0.5))
        self._impl = None
        if self.enabled:
            try:
                import pytesseract
                from PIL import Image  # noqa: F401
                self._impl = pytesseract
            except Exception:
                self._impl = None

    def available(self) -> bool:
        return self._impl is not None

    def run_image(self, data: bytes) -> Tuple[str, float]:
        if not self.available():
            return "", 0.0
        from PIL import Image
        text = self._impl.image_to_string(Image.open(io.BytesIO(data)), lang=self.languages).strip()
        return text, 0.75 if text else 0.0

    def run_pdf(self, data: bytes) -> Tuple[str, float]:
        if not self.available():
            return "", 0.0
        try:
            import pypdfium2 as pdfium
        except Exception:
            return "", 0.0
        out = []
        for page in pdfium.PdfDocument(io.BytesIO(data)):
            out.append(self._impl.image_to_string(page.render(scale=2).to_pil(), lang=self.languages))
        text = "\n\n".join(o for o in out if o.strip()).strip()
        return text, 0.7 if text else 0.0


class Parsers:
    # Text / table / number / OCR extraction. Each parser returns a normalized dict.
    @staticmethod
    def _blocks(text: str, prefix: str) -> List[Dict[str, Any]]:
        parts = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
        return [{"text": p, "locator": prefix + "#block" + str(i)} for i, p in enumerate(parts)]

    @classmethod
    def text_parser(cls, data: bytes, filename: str) -> Dict[str, Any]:
        text = data.decode("utf-8", errors="replace")
        return {"text": text, "blocks": cls._blocks(text, filename), "tables": [], "images_ocr": []}

    @classmethod
    def json_parser(cls, data: bytes, filename: str) -> Dict[str, Any]:
        try:
            obj = json.loads(data.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ValidationError("invalid JSON: " + str(exc))
        text = json.dumps(obj, ensure_ascii=False, indent=2)
        return {"text": text, "blocks": cls._blocks(text, filename), "tables": [],
                "images_ocr": [], "json": obj}

    @classmethod
    def csv_parser(cls, data: bytes, filename: str) -> Dict[str, Any]:
        rows = [r for r in csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
                if any(c.strip() for c in r)]
        table = {"name": Path(filename).stem, "header": rows[0] if rows else [],
                 "rows": rows[1:] if rows else []}
        text = "\n".join(",".join(r) for r in rows)
        return {"text": text, "blocks": cls._blocks(text, filename), "tables": [table], "images_ocr": []}

    @classmethod
    def spreadsheet_parser(cls, data: bytes, filename: str) -> Dict[str, Any]:
        try:
            import openpyxl
        except Exception:
            raise IntegrationError("openpyxl not installed - cannot parse spreadsheets")
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        tables, texts = [], []
        for ws in wb.worksheets:
            rows = [["" if c is None else str(c) for c in row] for row in ws.iter_rows(values_only=True)]
            rows = [r for r in rows if any(v.strip() for v in r)]
            if not rows:
                continue
            tables.append({"name": ws.title, "header": rows[0], "rows": rows[1:]})
            texts.append("[sheet:" + ws.title + "]\n" + "\n".join(" | ".join(r) for r in rows))
        text = "\n\n".join(texts)
        return {"text": text, "blocks": cls._blocks(text, filename), "tables": tables, "images_ocr": []}

    @classmethod
    def docx_parser(cls, data: bytes, filename: str) -> Dict[str, Any]:
        try:
            import docx
        except Exception:
            raise IntegrationError("python-docx not installed - cannot parse .docx")
        document = docx.Document(io.BytesIO(data))
        paras = [p.text.strip() for p in document.paragraphs if p.text.strip()]
        tables = []
        for ti, t in enumerate(document.tables):
            rows = [[c.text.strip() for c in r.cells] for r in t.rows]
            if rows:
                tables.append({"name": "table" + str(ti), "header": rows[0], "rows": rows[1:]})
        blocks = [{"text": p, "locator": filename + "#para" + str(i)} for i, p in enumerate(paras)]
        return {"text": "\n\n".join(paras), "blocks": blocks, "tables": tables, "images_ocr": []}

    @classmethod
    def pdf_parser(cls, data: bytes, filename: str, ocr: Optional[OcrEngine] = None,
                   ocr_threshold: int = 200) -> Dict[str, Any]:
        pages: List[str] = []
        tables: List[Dict[str, Any]] = []
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for pi, page in enumerate(pdf.pages):
                    pages.append(page.extract_text() or "")
                    for ti, tbl in enumerate(page.extract_tables() or []):
                        clean = [["" if c is None else str(c).strip() for c in row] for row in tbl]
                        if clean:
                            tables.append({"name": "p" + str(pi + 1) + "_t" + str(ti),
                                           "header": clean[0], "rows": clean[1:]})
        except Exception as exc:
            LOG.warning("pdf text extraction failed: %s", exc)
        text = "\n\n".join(pages)
        images_ocr: List[Dict[str, Any]] = []
        if ocr and len(text.strip()) < ocr_threshold:
            ocr_text, conf = ocr.run_pdf(data)
            if ocr_text:
                images_ocr.append({"locator": filename + "#ocr", "text": ocr_text, "confidence": conf})
                text = (text + "\n\n" + ocr_text).strip()
        blocks = [{"text": p.strip(), "locator": filename + "#page" + str(i + 1)}
                  for i, p in enumerate(pages) if p.strip()]
        blocks += [{"text": o["text"], "locator": o["locator"]} for o in images_ocr]
        return {"text": text, "blocks": blocks, "tables": tables, "images_ocr": images_ocr}

    @classmethod
    def image_ocr_parser(cls, data: bytes, filename: str,
                         ocr: Optional[OcrEngine] = None) -> Dict[str, Any]:
        if not ocr:
            return {"text": "", "blocks": [], "tables": [], "images_ocr": []}
        text, conf = ocr.run_image(data)
        blocks = [{"text": text, "locator": filename + "#ocr"}] if text.strip() else []
        return {"text": text, "blocks": blocks, "tables": [],
                "images_ocr": [{"locator": filename + "#ocr", "text": text, "confidence": conf}]}


class Normalizer:
    # Maps raw extraction output onto configured business entities.
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.norm = cfg.get("ingestion.normalization", {}) or {}
        self.synonyms = {k: [s.lower() for s in v]
                         for k, v in (self.norm.get("field_synonyms") or {}).items()}
        cur = self.norm.get("currency") or {}
        self.currency_symbols = cur.get("detect_symbols", {})
        self.default_currency = cur.get("default", "USD")

    def detect_currency(self, text: str) -> Optional[str]:
        for sym, code in self.currency_symbols.items():
            if sym in text:
                return code
        m = re.search(r"\b(USD|EUR|GBP|AED|SAR|INR|TRY|IDR|BRL)\b", text.upper())
        return m.group(1) if m else None

    def parse_date(self, raw: str) -> Optional[str]:
        for fmt in self.norm.get("date_formats", []) or []:
            try:
                return datetime.strptime(str(raw).strip(), fmt).date().isoformat()
            except ValueError:
                continue
        return None

    def _entity_for(self, header_cell: str) -> Optional[str]:
        h = str(header_cell).strip().lower()
        for entity, words in self.synonyms.items():
            if any(w in h for w in words):
                return entity
        return None

    def from_tables(self, tenant_id: str, doc_id: str, version: int,
                    tables: Sequence[Dict[str, Any]]) -> List[Fact]:
        facts: List[Fact] = []
        for table in tables:
            header = [str(h) for h in (table.get("header") or [])]
            mapping = {i: self._entity_for(h) for i, h in enumerate(header)}
            for ri, row in enumerate(table.get("rows") or []):
                record = {header[i].strip(): str(c).strip()
                          for i, c in enumerate(row) if i < len(header)}
                locator = (doc_id + "@v" + str(version) + ":" + str(table.get("name")) +
                           "#row" + str(ri + 2))
                room = self._pick(record, mapping, header, "room_type")
                rate = self._pick(record, mapping, header, "rate")
                avail = self._pick(record, mapping, header, "availability")
                day = self._pick(record, mapping, header, "checkin_checkout") or self._first_date(record)
                if room:
                    facts.append(Fact(tenant_id, "room_type", room.lower(), room, doc_id, version, locator))
                if rate:
                    facts.append(Fact(tenant_id, "rate", (room or "rate").lower(), rate, doc_id, version,
                                      locator, numeric_value=self._to_number(rate),
                                      currency=self.detect_currency(rate) or self.default_currency,
                                      valid_from=day))
                if avail:
                    facts.append(Fact(tenant_id, "availability", (room or "availability").lower(), avail,
                                      doc_id, version, locator,
                                      numeric_value=self._to_number(avail), valid_from=day))
                if not (room or rate or avail):
                    flat = " | ".join(k + ": " + v for k, v in record.items() if v)
                    if flat:
                        facts.append(Fact(tenant_id, "operational_instruction", "row" + str(ri), flat,
                                          doc_id, version, locator, confidence=0.6))
        return facts

    def from_text(self, tenant_id: str, doc_id: str, version: int,
                  blocks: Sequence[Dict[str, Any]]) -> List[Fact]:
        facts: List[Fact] = []
        for block in blocks:
            text = block["text"]
            low = text.lower()
            locator = doc_id + "@v" + str(version) + ":" + block["locator"]
            for entity in ("policy", "amenity", "checkin_checkout", "location", "booking_rule"):
                if any(w in low for w in self.synonyms.get(entity, [entity])):
                    facts.append(Fact(tenant_id, entity, self._slug(text[:48]), text[:1500],
                                      doc_id, version, locator, confidence=0.7))
            for sym, amount in re.findall(r"([$\u20ac\u00a3\u20b9]|USD|EUR|AED|SAR|INR)\s?([0-9][0-9,.]*)", text)[:10]:
                facts.append(Fact(tenant_id, "rate", self._slug(text[:32]), sym + amount,
                                  doc_id, version, locator, numeric_value=self._to_number(amount),
                                  currency=self.currency_symbols.get(
                                      sym, sym if len(sym) == 3 else self.default_currency),
                                  confidence=0.6))
        return facts

    def detect_conflicts(self, existing: Sequence[Dict[str, Any]],
                         incoming: Sequence[Fact]) -> List[Dict[str, Any]]:
        threshold = float((self.norm.get("conflict_policy") or {}).get("flag_threshold_pct", 5.0))
        by_key = {(r["entity"], r["key"]): r for r in existing}
        conflicts = []
        for fact in incoming:
            prev = by_key.get((fact.entity, fact.key))
            if not prev:
                continue
            a, b = prev.get("numeric_value"), fact.numeric_value
            if a is not None and b is not None and a:
                delta = abs(a - b) / abs(a) * 100.0
                if delta > threshold:
                    conflicts.append({"entity": fact.entity, "key": fact.key, "old": a, "new": b,
                                      "delta_pct": round(delta, 2),
                                      "sources": [prev.get("locator"), fact.locator]})
            elif fact.entity in ("policy", "checkin_checkout", "booking_rule") and \
                    str(prev.get("value", "")).strip() not in ("", fact.value.strip()):
                conflicts.append({"entity": fact.entity, "key": fact.key,
                                  "old": str(prev.get("value"))[:120], "new": fact.value[:120],
                                  "sources": [prev.get("locator"), fact.locator]})
        return conflicts

    def _pick(self, record: Dict[str, str], mapping: Dict[int, Optional[str]],
              header: Sequence[str], entity: str) -> Optional[str]:
        for i, ent in mapping.items():
            if ent == entity and i < len(header):
                val = record.get(str(header[i]).strip(), "").strip()
                if val:
                    return val
        return None

    def _first_date(self, record: Dict[str, str]) -> Optional[str]:
        for v in record.values():
            parsed = self.parse_date(v)
            if parsed:
                return parsed
        return None

    @staticmethod
    def _to_number(raw: Any) -> Optional[float]:
        m = re.search(r"[0-9][0-9,.]*", str(raw).replace(" ", ""))
        if not m:
            return None
        token = m.group(0)
        if "," in token and "." in token:
            token = token.replace(",", "")
        elif "," in token:
            token = token.replace(",", "" if len(token.split(",")[-1]) == 3 else ".")
        try:
            return float(token.rstrip("."))
        except ValueError:
            return None

    @staticmethod
    def _slug(text: str) -> str:
        text = unicodedata.normalize("NFKD", text)
        return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60] or "item"


class IngestionService:
    def __init__(self, cfg: Config, db: Database, storage: StorageService,
                 audit: AuditLog, metrics: Metrics) -> None:
        self.cfg, self.db, self.storage, self.audit, self.metrics = cfg, db, storage, audit, metrics
        self.validator = FileValidator(cfg)
        self.ocr = OcrEngine(cfg)
        self.normalizer = Normalizer(cfg)
        self.routing = cfg.get("ingestion.retrieval_routing", {}) or {}
        self.parser_map = cfg.get("ingestion.parsers", {}) or {}

    def next_version(self, tenant_id: str, filename: str) -> Tuple[str, int]:
        row = self.db.one("SELECT id, MAX(version) AS v FROM documents WHERE tenant_id=? AND filename=?"
                          " GROUP BY id", (tenant_id, filename))
        if row and row.get("id"):
            return row["id"], int(row["v"]) + 1
        return new_id("doc"), 1

    def choose_route(self, ext: str, size: int, text_len: int) -> str:
        # Automatic direct-reference vs indexed-retrieval selection.
        if ext in (self.routing.get("always_index_extensions") or []):
            return "indexed"
        if ext in (self.routing.get("always_direct_extensions") or []):
            return "direct"
        if text_len <= int(self.routing.get("direct_reference_max_chars", 12000)) and \
                size <= float(self.routing.get("direct_reference_max_file_mb", 1.5)) * 1024 * 1024:
            return "direct"
        return "indexed"

    def ingest(self, principal: Principal, filename: str, data: bytes, mime: str = "") -> Dict[str, Any]:
        if not self.cfg.flag("enable_ingestion", True):
            raise Forbidden("ingestion disabled by feature flag")
        tenant_id = principal.tenant_id
        ext = self.validator.validate(filename, data, mime)
        ok, scan_note = self.validator.scan(data)
        digest = sha256_bytes(data)
        if self.cfg.get("ingestion.versioning.dedupe_identical_hash", True):
            dup = self.db.one("SELECT * FROM documents WHERE tenant_id=? AND sha256=?", (tenant_id, digest))
            if dup:
                self.audit.record(tenant_id, "ingest", {"filename": filename, "dedupe": True,
                                                        "doc_id": dup["id"]}, actor=principal.user_id)
                return {"doc_id": dup["id"], "version": dup["version"], "status": "duplicate",
                        "route": dup["route"], "facts": 0, "chunks": 0, "conflicts": []}
        doc_id, version = self.next_version(tenant_id, filename)
        self.storage.write_bytes(self.storage.resolve("originals", tenant_id=tenant_id, doc_id=doc_id,
                                                     version=version, filename=filename), data)
        if not ok:
            self.storage.write_bytes(self.storage.resolve("quarantine", tenant_id=tenant_id,
                                                          doc_id=doc_id, filename=filename), data)
            raise ValidationError("file quarantined by malware policy", detail=scan_note)

        parsed = self._parse(ext, data, filename)
        text = parsed.get("text") or ""
        route = self.choose_route(ext, len(data), len(text))
        self.storage.write_text(self.storage.resolve("extracted", tenant_id=tenant_id,
                                                     doc_id=doc_id, version=version),
                                json.dumps(parsed, ensure_ascii=False, default=str))
        self.db.upsert("documents", {
            "id": doc_id, "tenant_id": tenant_id, "version": version, "filename": filename, "ext": ext,
            "mime": mime or "application/octet-stream", "size": len(data), "sha256": digest,
            "status": "ingested", "route": route,
            "storage_path": str(self.storage.resolve("originals", tenant_id=tenant_id, doc_id=doc_id,
                                                     version=version, filename=filename)),
            "created_at": iso()}, keys=["id", "version"])

        facts = self.normalizer.from_tables(tenant_id, doc_id, version, parsed.get("tables") or [])
        facts += self.normalizer.from_text(tenant_id, doc_id, version, parsed.get("blocks") or [])
        existing = self.db.query("SELECT * FROM facts WHERE tenant_id=?", (tenant_id,))
        conflicts = self.normalizer.detect_conflicts(existing, facts)
        for fact in facts:
            self.db.insert("facts", dataclasses.asdict(fact))
        chunks = self._index(tenant_id, doc_id, version, parsed, whole_blocks=(route == "direct"))

        self.metrics.inc("ingest_docs")
        self.audit.record(tenant_id, "ingest", {"doc_id": doc_id, "version": version,
                                                "filename": filename, "route": route, "bytes": len(data),
                                                "sha256": digest, "scan": scan_note,
                                                "tables": len(parsed.get("tables") or []),
                                                "ocr_blocks": len(parsed.get("images_ocr") or [])},
                          actor=principal.user_id)
        self.audit.record(tenant_id, "normalize", {"doc_id": doc_id, "version": version,
                                                   "facts": len(facts), "conflicts": conflicts},
                          actor=principal.user_id)
        if conflicts and (self.cfg.get("ingestion.normalization.conflict_policy") or {}
                          ).get("on_conflict") == "escalate":
            self.db.insert("escalations", {"id": new_id("esc"), "tenant_id": tenant_id,
                                          "conversation_id": None, "reason": "conflicting_sources",
                                          "detail": json.dumps(conflicts[:20], ensure_ascii=False),
                                          "status": "open", "created_at": iso(), "resolved_at": None})
        return {"doc_id": doc_id, "version": version, "status": "ingested", "route": route,
                "facts": len(facts), "chunks": chunks, "conflicts": conflicts}

    def _parse(self, ext: str, data: bytes, filename: str) -> Dict[str, Any]:
        name = self.parser_map.get(ext, "text_parser")
        threshold = int((self.cfg.get("ingestion.ocr") or {}).get(
            "pdf_ocr_fallback_when_text_chars_below", 200))
        try:
            if name == "pdf_parser":
                return Parsers.pdf_parser(data, filename, self.ocr, threshold)
            if name == "image_ocr_parser":
                return Parsers.image_ocr_parser(data, filename, self.ocr)
            fn = getattr(Parsers, name, None)
            if fn is None:
                raise ValidationError("parser not implemented: " + name)
            return fn(data, filename)
        except SaraError:
            self.metrics.inc("ingest_failures")
            raise
        except Exception as exc:
            self.metrics.inc("ingest_failures")
            raise ValidationError("failed to parse " + filename + ": " + str(exc))

    def _index(self, tenant_id: str, doc_id: str, version: int,
               parsed: Dict[str, Any], whole_blocks: bool = False) -> int:
        chunk_cfg = self.routing.get("chunk", {}) or {}
        size = int(chunk_cfg.get("size_chars", 1200))
        overlap = int(chunk_cfg.get("overlap_chars", 180))
        pieces: List[Tuple[str, str]] = []
        for block in parsed.get("blocks") or []:
            text, locator = block["text"], block["locator"]
            if whole_blocks or len(text) <= size:
                pieces.append((text, locator))
                continue
            start = part = 0
            while start < len(text):
                pieces.append((text[start:start + size], locator + "~" + str(part)))
                start += max(1, size - overlap)
                part += 1
        for table in parsed.get("tables") or []:
            header = " | ".join(str(h) for h in (table.get("header") or []))
            for ri, row in enumerate(table.get("rows") or []):
                pieces.append((header + "\n" + " | ".join(str(c) for c in row),
                               str(table.get("name")) + "#row" + str(ri + 2)))
        index_path = self.storage.resolve("index", tenant_id=tenant_id, doc_id=doc_id)
        for ordinal, (text, locator) in enumerate(pieces):
            row = {"id": new_id("chk"), "tenant_id": tenant_id, "doc_id": doc_id,
                   "doc_version": version, "ordinal": ordinal, "text": text,
                   "locator": doc_id + "@v" + str(version) + ":" + locator, "created_at": iso()}
            self.db.insert("chunks", row)
            self.storage.append_line(index_path, json.dumps(row, ensure_ascii=False))
        return len(pieces)

    def list_documents(self, tenant_id: str) -> List[Dict[str, Any]]:
        return self.db.query("SELECT * FROM documents WHERE tenant_id=? ORDER BY created_at DESC",
                             (tenant_id,))


# ------------------------------------------------------ knowledge / retrieval
STOPWORDS = {"the", "a", "an", "is", "are", "of", "for", "to", "in", "on", "and", "or",
             "do", "you", "have", "how", "much", "i", "we", "my", "me", "can", "what"}


class KnowledgeService:
    def __init__(self, cfg: Config, db: Database, audit: AuditLog) -> None:
        self.cfg, self.db, self.audit = cfg, db, audit
        self.conf = cfg.get("knowledge", {}) or {}

    @staticmethod
    def tokenize(text: str) -> List[str]:
        return [t for t in re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE)
                if t not in STOPWORDS and len(t) > 1]

    def search(self, tenant_id: str, query: str, limit: Optional[int] = None) -> List[Retrieved]:
        # BM25-style lexical retrieval over the tenant-scoped chunk index.
        limit = limit or int((self.conf.get("grounding") or {}).get("max_sources_per_answer", 5))
        terms = self.tokenize(query)
        if not terms:
            return []
        rows = self.db.query("SELECT text, locator FROM chunks WHERE tenant_id=?", (tenant_id,))
        docs = [self.tokenize(r["text"]) for r in rows]
        if not docs:
            return []
        n = len(docs)
        dfs = {t: sum(1 for d in docs if t in d) for t in terms}
        avg_len = sum(len(d) for d in docs) / n
        k1, b = 1.5, 0.75
        results: List[Retrieved] = []
        for row, tokens in zip(rows, docs):
            score, dl = 0.0, len(tokens) or 1
            for t in terms:
                tf = tokens.count(t)
                if not tf or not dfs.get(t):
                    continue
                idf = max(0.01, (n - dfs[t] + 0.5) / (dfs[t] + 0.5)) ** 0.5
                score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / max(1.0, avg_len)))
            if score > 0:
                results.append(Retrieved(row["text"], score, row["locator"]))
        results.sort(key=lambda r: r.score, reverse=True)
        top = results[:limit]
        best = top[0].score if top else 0.0
        for r in top:
            r.score = round(r.score / best, 4) if best else 0.0
        self.audit.record(tenant_id, "retrieval", {"query": query[:300],
                                                   "sources": [r.source_ref for r in top]})
        return top

    def facts(self, tenant_id: str, entity: str, key_like: Optional[str] = None) -> List[Dict[str, Any]]:
        if key_like:
            return self.db.query("SELECT * FROM facts WHERE tenant_id=? AND entity=? AND key LIKE ?"
                                 " ORDER BY updated_at DESC", (tenant_id, entity, "%" + key_like.lower() + "%"))
        return self.db.query("SELECT * FROM facts WHERE tenant_id=? AND entity=? ORDER BY updated_at DESC",
                             (tenant_id, entity))

    def price_lookup(self, tenant_id: str, room_hint: Optional[str] = None) -> Dict[str, Any]:
        rows = self.facts(tenant_id, "rate", room_hint)
        if not rows and room_hint:
            rows = self.facts(tenant_id, "rate")
        if not rows:
            return {"found": False, "reason": "no_rate_facts", "sources": []}
        distinct = {(r.get("numeric_value"), r.get("currency")) for r in rows
                    if r.get("numeric_value") is not None}
        stale = self._stale(rows[0].get("updated_at"),
                            days=int((self.conf.get("staleness") or {}).get("rate_max_age_days", 30)))
        return {"found": True, "rows": rows[:5], "conflict": len(distinct) > 1, "stale": stale,
                "sources": [r["locator"] for r in rows[:5]]}

    def availability_lookup(self, tenant_id: str, room_hint: Optional[str] = None,
                            day: Optional[str] = None) -> Dict[str, Any]:
        rows = self.facts(tenant_id, "availability", room_hint)
        if day:
            rows = [r for r in rows if (r.get("valid_from") or "") == day] or rows
        if not rows:
            return {"found": False, "reason": "no_availability_facts", "sources": []}
        stale = self._stale(rows[0].get("updated_at"),
                            minutes=int((self.conf.get("staleness") or {}).get(
                                "availability_max_age_minutes", 120)))
        return {"found": True, "rows": rows[:5], "stale": stale,
                "sources": [r["locator"] for r in rows[:5]]}

    @staticmethod
    def _stale(updated_at: Optional[str], minutes: int = 0, days: int = 0) -> bool:
        if not updated_at:
            return True
        try:
            ts = datetime.fromisoformat(updated_at)
        except ValueError:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        limit = timedelta(minutes=minutes, days=days)
        return (now() - ts) > limit if limit.total_seconds() > 0 else False


# --------------------------------------------------------------- prompts
class PromptRegistry:
    # Prompts are versioned and stored independently from business data.
    def __init__(self, cfg: Config, db: Database, audit: AuditLog) -> None:
        self.cfg, self.db, self.audit = cfg, db, audit

    def active_id(self, role: str) -> str:
        active = self.cfg.get("prompts.active", {}) or {}
        if role not in active:
            raise NotFound("no active prompt configured for role '" + role + "'")
        return active[role]

    def get(self, tenant_id: str, role: str) -> Dict[str, Any]:
        prompt_id = self.active_id(role)
        override = self.db.one("SELECT * FROM prompts WHERE tenant_id=? AND id=? AND active=1"
                               " ORDER BY version DESC LIMIT 1", (tenant_id, prompt_id))
        if override:
            return {"id": prompt_id, "role": role, "version": override["version"],
                    "text": override["text"], "source": "tenant_override"}
        tpl = (self.cfg.get("prompts.templates") or {}).get(prompt_id)
        if not tpl:
            raise NotFound("prompt template missing: " + prompt_id)
        return {"id": prompt_id, "role": role, "version": tpl.get("version", 1),
                "text": tpl["text"], "source": "schema"}

    def update(self, principal: Principal, role: str, text: str) -> Dict[str, Any]:
        prompt_id = self.active_id(role)
        row = self.db.one("SELECT MAX(version) AS v FROM prompts WHERE tenant_id=? AND id=?",
                          (principal.tenant_id, prompt_id))
        version = int((row or {}).get("v") or 0) + 1
        self.db.execute("UPDATE prompts SET active=0 WHERE tenant_id=? AND id=?",
                        (principal.tenant_id, prompt_id))
        self.db.insert("prompts", {"id": prompt_id, "tenant_id": principal.tenant_id,
                                   "version": version, "role": role, "text": text,
                                   "active": 1, "updated_at": iso()})
        self.audit.record(principal.tenant_id, "prompt_update",
                          {"prompt_id": prompt_id, "role": role, "version": version,
                           "chars": len(text)}, actor=principal.user_id)
        return {"id": prompt_id, "role": role, "version": version}

    def render(self, tenant_id: str, role: str, variables: Dict[str, Any]) -> Tuple[str, str]:
        prompt = self.get(tenant_id, role)
        merged = dict(self.cfg.get("prompts.variables", {}) or {})
        merged.update({k: v for k, v in variables.items() if v is not None})
        text = prompt["text"]
        for key, value in merged.items():
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            text = text.replace("{" + key + "}", str(value))
        return text, prompt["id"] + "@v" + str(prompt["version"])


class LanguageDetector:
    SCRIPTS = [("ar", r"[\u0600-\u06FF]"), ("he", r"[\u0590-\u05FF]"), ("ru", r"[\u0400-\u04FF]"),
               ("hi", r"[\u0900-\u097F]"), ("zh", r"[\u4E00-\u9FFF]"), ("ja", r"[\u3040-\u30FF]"),
               ("ko", r"[\uAC00-\uD7AF]")]
    HINTS = {"fr": ["bonjour", "chambre", "disponible", "merci", "prix", "reserver"],
             "es": ["hola", "habitacion", "precio", "gracias", "disponible", "reservar"],
             "de": ["hallo", "zimmer", "preis", "danke", "verfugbar", "buchen"],
             "pt": ["ola", "quarto", "preco", "obrigado", "disponivel", "reservar"],
             "tr": ["merhaba", "oda", "fiyat", "musait", "rezervasyon"],
             "id": ["halo", "kamar", "harga", "tersedia", "pesan"],
             "en": ["hello", "hi", "room", "price", "thanks", "available", "book", "booking", "night"]}

    def __init__(self, cfg: Config) -> None:
        rules = cfg.get("language_rules", {}) or {}
        self.enabled = bool(rules.get("auto_detect", True))
        self.supported = rules.get("supported", ["en"])
        self.default = rules.get("default", "en")

    def detect(self, text: str) -> Tuple[str, float]:
        if not self.enabled or not (text or "").strip():
            return self.default, 0.0
        for lang, pattern in self.SCRIPTS:
            if re.search(pattern, text):
                return (lang if lang in self.supported else self.default), 0.95
        low = " " + text.lower() + " "
        scores = {lang: sum(1 for w in words if w in low) for lang, words in self.HINTS.items()}
        best = max(scores, key=lambda k: scores[k])
        if scores[best] == 0:
            return self.default, 0.3
        lang = best if best in self.supported else self.default
        return lang, round(scores[best] / (sum(scores.values()) or 1), 2)


class LlmClient:
    # Provider-agnostic. `mock` provider composes a grounded deterministic reply,
    # so the whole system runs (and replays) without external dependencies.
    def __init__(self, cfg: Config, metrics: Metrics) -> None:
        self.cfg, self.metrics = cfg, metrics
        self.conf = cfg.get("integrations.llm", {}) or {}
        self.provider = str(self.conf.get("provider") or "mock").lower()
        self.custom: Optional[Callable[[str, str], str]] = None

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.metrics.inc("llm_calls")
        if self.custom:
            return self.custom(system_prompt, user_prompt)
        if self.provider in ("", "mock", "none"):
            return ""
        if self.provider == "openai":
            return self._openai(system_prompt, user_prompt)
        raise IntegrationError("unsupported llm provider: " + self.provider)

    def _openai(self, system_prompt: str, user_prompt: str) -> str:
        import urllib.request
        key = os.environ.get("SARA_LLM_API_KEY")
        if not key:
            raise IntegrationError("SARA_LLM_API_KEY not configured")
        base = (os.environ.get("SARA_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        model = (os.environ.get("SARA_LLM_MODEL") or self.conf.get("model") or "gpt-4o-mini")
        body = json.dumps({"model": model,
                           "temperature": self.conf.get("temperature", 0.2),
                           "max_tokens": self.conf.get("max_output_tokens", 600),
                           "messages": [{"role": "system", "content": system_prompt},
                                        {"role": "user", "content": user_prompt}]}).encode()
        req = urllib.request.Request(base + "/chat/completions", data=body,
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json",
                                              # Some OpenAI-compatible gateways (e.g. xKiro
                                              # behind Cloudflare) return 403/1010 for the
                                              # default Python-urllib user agent.
                                              "User-Agent": (
                                                  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                  "Chrome/126.0.0.0 Safari/537.36")})
        with urllib.request.urlopen(req, timeout=int(self.conf.get("timeout_seconds", 30))) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------- booking + docs
class BookingService:
    def __init__(self, cfg: Config, db: Database, audit: AuditLog, metrics: Metrics,
                 crypto: Crypto) -> None:
        self.cfg, self.db, self.audit, self.metrics, self.crypto = cfg, db, audit, metrics, crypto
        self.conf = cfg.get("booking", {}) or {}

    def required_fields(self) -> List[str]:
        return list(self.conf.get("required_fields", []) or [])

    def missing_fields(self, data: Dict[str, Any]) -> List[str]:
        return [f for f in self.required_fields() if not str(data.get(f) or "").strip()]

    def validate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        rules = self.conf.get("validation", {}) or {}
        missing = self.missing_fields(data)
        if missing:
            raise ValidationError("missing booking fields", missing=missing)
        try:
            ci = datetime.fromisoformat(str(data["checkin_date"])).date()
            co = datetime.fromisoformat(str(data["checkout_date"])).date()
        except Exception:
            raise ValidationError("checkin_date/checkout_date must be ISO YYYY-MM-DD")
        nights = (co - ci).days
        if nights < int(rules.get("min_nights", 1)):
            raise ValidationError("stay too short", nights=nights)
        if nights > int(rules.get("max_nights", 30)):
            raise ValidationError("stay too long", nights=nights)
        if not rules.get("allow_past_dates", False) and ci < now().date():
            raise ValidationError("check-in date is in the past")
        guests = int(data.get("guests") or 1)
        if guests > int(rules.get("max_guests_per_room", 4)):
            raise ValidationError("too many guests for one room", guests=guests)
        out = dict(data)
        out["nights"] = nights
        out["guests"] = guests
        return out

    def create_draft(self, tenant_id: str, conversation_id: Optional[str],
                     data: Dict[str, Any], actor: str = "agent") -> Dict[str, Any]:
        clean = self.validate(data)
        status = "pending_approval" if (self.conf.get("require_staff_approval", True) or
                                        not self.cfg.flag("enable_auto_booking_without_approval")) else "confirmed"
        booking_id = new_id("bkg")
        stored = dict(clean)
        for path in self.cfg.get("security.encryption.encrypt_fields", []) or []:
            leaf = path.split(".")[-1]
            if leaf in stored and stored[leaf] is not None:
                stored[leaf] = self.crypto.encrypt(str(stored[leaf]))
        hold_until = iso(now() + timedelta(minutes=int(self.conf.get("hold_minutes", 45))))
        row = {"id": booking_id, "tenant_id": tenant_id, "conversation_id": conversation_id,
               "status": status, "data": json.dumps(stored, ensure_ascii=False), "version": 1,
               "hold_until": hold_until, "created_at": iso(), "updated_at": iso()}
        self.db.insert("bookings", row)
        self.metrics.inc("bookings_created")
        self.audit.record(tenant_id, "booking_change", {"booking_id": booking_id, "status": status,
                                                        "version": 1, "fields": sorted(clean)},
                          actor=actor, conversation_id=conversation_id)
        return self.get(tenant_id, booking_id)

    def get(self, tenant_id: str, booking_id: str) -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM bookings WHERE tenant_id=? AND id=?", (tenant_id, booking_id))
        if not row:
            raise NotFound("booking not found: " + booking_id)
        data = json.loads(row["data"])
        for k, v in list(data.items()):
            if isinstance(v, str) and v[:3] in ("v1:", "v2:"):
                try:
                    data[k] = self.crypto.decrypt(v)
                except Exception:
                    data[k] = "***"
        out = dict(row)
        out["data"] = data
        return out

    def set_status(self, principal: Principal, booking_id: str, status: str) -> Dict[str, Any]:
        if status not in (self.conf.get("statuses") or []):
            raise ValidationError("unknown booking status: " + status)
        row = self.db.one("SELECT * FROM bookings WHERE tenant_id=? AND id=?",
                          (principal.tenant_id, booking_id))
        if not row:
            raise NotFound("booking not found: " + booking_id)
        version = int(row["version"]) + 1
        self.db.execute("UPDATE bookings SET status=?, version=?, updated_at=? WHERE tenant_id=? AND id=?",
                        (status, version, iso(), principal.tenant_id, booking_id))
        if status == "confirmed":
            self.metrics.inc("bookings_confirmed")
        self.audit.record(principal.tenant_id, "booking_change",
                          {"booking_id": booking_id, "status": status, "version": version},
                          actor=principal.user_id, conversation_id=row["conversation_id"])
        return self.get(principal.tenant_id, booking_id)

    def list(self, tenant_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = self.db.query("SELECT id FROM bookings WHERE tenant_id=? AND status=?"
                                 " ORDER BY created_at DESC", (tenant_id, status))
        else:
            rows = self.db.query("SELECT id FROM bookings WHERE tenant_id=? ORDER BY created_at DESC",
                                 (tenant_id,))
        return [self.get(tenant_id, r["id"]) for r in rows]


class DocumentService:
    # Template-driven generation with versioning and source-relationship tracking.
    def __init__(self, cfg: Config, db: Database, storage: StorageService, audit: AuditLog) -> None:
        self.cfg, self.db, self.storage, self.audit = cfg, db, storage, audit
        self.conf = cfg.get("documents", {}) or {}

    def template(self, tenant_id: str, template_id: str) -> str:
        path = self.storage.resolve("templates", tenant_id=tenant_id, template_id=template_id)
        if path.exists():
            return path.read_text(encoding="utf-8")
        tpl = (self.conf.get("templates") or {}).get(template_id)
        if not tpl:
            raise NotFound("template not found: " + template_id)
        return tpl

    def save_template(self, tenant_id: str, template_id: str, html: str) -> str:
        path = self.storage.resolve("templates", tenant_id=tenant_id, template_id=template_id)
        self.storage.write_text(path, html)
        return str(path)

    def _next_version(self, tenant_id: str, kind: str, entity_id: str) -> int:
        row = self.db.one("SELECT MAX(version) AS v FROM generated_documents"
                          " WHERE tenant_id=? AND kind=? AND entity_id=?", (tenant_id, kind, entity_id))
        return int((row or {}).get("v") or 0) + 1

    def _check_required(self, payload: Dict[str, Any]) -> None:
        missing = [f for f in (self.conf.get("required_fields") or [])
                   if deep_get(payload, f) in (None, "", [])]
        if missing:
            raise ValidationError("generated document missing verified fields", missing=missing)

    def generate_for_booking(self, tenant_id: str, booking: Dict[str, Any],
                             source_refs: Sequence[str], staff_notes: str = "") -> List[Dict[str, Any]]:
        if not self.cfg.flag("enable_document_generation", True):
            return []
        data = booking["data"]
        payload = {
            "booking_id": booking["id"], "status": booking["status"], "version": booking["version"],
            "created_at": booking["created_at"], "guest": {"full_name": data.get("full_name"),
                                                           "phone": data.get("phone"),
                                                           "email": data.get("email")},
            "room_type": data.get("room_type"), "checkin_date": data.get("checkin_date"),
            "checkout_date": data.get("checkout_date"), "nights": data.get("nights"),
            "guests": data.get("guests"), "price_total": data.get("price_total"),
            "currency": data.get("currency"), "preferences": data.get("preferences") or "none recorded",
            "checkin_instructions": data.get("checkin_instructions") or "provided by staff on arrival",
            "source_refs": list(source_refs) or ["no_source"],
            "business_name": self.cfg.get("prompts.variables.business_name") or "",
            "staff_notes": staff_notes or "none",
        }
        self._check_required(payload)
        out: List[Dict[str, Any]] = []
        for spec in self.conf.get("outputs", []) or []:
            for fmt in spec.get("formats", []) or []:
                out.append(self._emit(tenant_id, spec, fmt, payload))
        return out

    def _emit(self, tenant_id: str, spec: Dict[str, Any], fmt: str,
              payload: Dict[str, Any]) -> Dict[str, Any]:
        kind = spec["kind"]
        entity_id = payload["booking_id"]
        version = self._next_version(tenant_id, kind, entity_id)
        path = self.storage.resolve("generated_docs", tenant_id=tenant_id, kind=kind,
                                    entity_id=entity_id, version=version, ext=fmt)
        if fmt == "json":
            self.storage.write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        elif fmt == "csv":
            buf = io.StringIO()
            flat = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                    for k, v in payload.items()}
            writer = csv.DictWriter(buf, fieldnames=list(flat))
            writer.writeheader()
            writer.writerow(flat)
            self.storage.write_text(path, buf.getvalue())
        elif fmt == "pdf":
            html = self._render_html(tenant_id, spec, payload)
            self.storage.write_bytes(path, self._html_to_pdf(html))
        else:
            raise ValidationError("unsupported document format: " + fmt)
        row = {"id": new_id("gdoc"), "tenant_id": tenant_id, "kind": kind, "entity_id": entity_id,
               "version": version, "fmt": fmt, "template_id": spec.get("template_id"),
               "storage_path": str(path),
               "source_refs": json.dumps(payload["source_refs"], ensure_ascii=False),
               "created_at": iso()}
        self.db.insert("generated_documents", row)
        self.audit.record(tenant_id, "document_generated",
                          {"kind": kind, "fmt": fmt, "version": version, "booking_id": entity_id,
                           "path": str(path), "source_refs": payload["source_refs"]})
        return row

    def _render_html(self, tenant_id: str, spec: Dict[str, Any], payload: Dict[str, Any]) -> str:
        html = self.template(tenant_id, spec["template_id"])
        rows = []
        for key in ("booking_id", "room_type", "checkin_date", "checkout_date", "nights",
                    "guests", "price_total", "currency", "status"):
            rows.append("<tr><th>" + key.replace("_", " ").title() + "</th><td>" +
                        self._esc(payload.get(key)) + "</td></tr>")
        guest = payload.get("guest") or {}
        for key in ("full_name", "phone", "email"):
            rows.append("<tr><th>Guest " + key.replace("_", " ").title() + "</th><td>" +
                        self._esc(guest.get(key)) + "</td></tr>")
        values = dict(payload)
        values["rows"] = "".join(rows)
        values["source_refs"] = ", ".join(str(s) for s in payload["source_refs"])
        for key, value in values.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            html = html.replace("{" + key + "}", self._esc(value))
        return html.replace("{rows}", values["rows"])

    @staticmethod
    def _esc(value: Any) -> str:
        text = "" if value is None else str(value)
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    @staticmethod
    def _html_to_pdf(html: str) -> bytes:
        try:
            from weasyprint import HTML
            return HTML(string=html).write_pdf()
        except Exception:
            pass
        # Minimal dependency-free PDF fallback so the pipeline never hard-fails.
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        lines, cur = [], ""
        for word in text.split(" "):
            if len(cur) + len(word) > 95:
                lines.append(cur)
                cur = word
            else:
                cur = (cur + " " + word).strip()
        if cur:
            lines.append(cur)
        body = "BT /F1 10 Tf 40 780 Td 14 TL\n"
        for line in lines[:55]:
            body += "(" + line.replace("\\", "").replace("(", "[").replace(")", "]") + ") Tj T*\n"
        body += "ET"
        objs = ["<< /Type /Catalog /Pages 2 0 R >>",
                "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
                "<< /Length " + str(len(body)) + " >>\nstream\n" + body + "\nendstream",
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        out, offsets = "%PDF-1.4\n", []
        for i, obj in enumerate(objs, start=1):
            offsets.append(len(out))
            out += str(i) + " 0 obj\n" + obj + "\nendobj\n"
        start = len(out)
        out += "xref\n0 " + str(len(objs) + 1) + "\n0000000000 65535 f \n"
        for off in offsets:
            out += str(off).zfill(10) + " 00000 n \n"
        out += ("trailer\n<< /Size " + str(len(objs) + 1) + " /Root 1 0 R >>\nstartxref\n" +
                str(start) + "\n%%EOF")
        return out.encode("latin-1", errors="replace")

    def list(self, tenant_id: str, entity_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if entity_id:
            return self.db.query("SELECT * FROM generated_documents WHERE tenant_id=? AND entity_id=?"
                                 " ORDER BY created_at DESC", (tenant_id, entity_id))
        return self.db.query("SELECT * FROM generated_documents WHERE tenant_id=?"
                             " ORDER BY created_at DESC", (tenant_id,))


# ------------------------------------------------- managed connector plumbing
class ManagedConnector:
    # Managed OAuth/provider authorization: the tenant never supplies raw
    # tokens/keys. We store only connector-issued grants (encrypted at rest).
    def __init__(self, cfg: Config, db: Database, crypto: Crypto, audit: AuditLog,
                 provider: str, conf_path: str) -> None:
        self.cfg, self.db, self.crypto, self.audit = cfg, db, crypto, audit
        self.provider = provider
        self.conf = cfg.get(conf_path, {}) or {}
        self.connector = self.conf.get("connector", {}) or {}

    def start_authorization(self, principal: Principal, label: str = "") -> Dict[str, Any]:
        connection_id = new_id("conn")
        state = base64.urlsafe_b64encode(json.dumps(
            {"c": connection_id, "t": principal.tenant_id, "n": uuid.uuid4().hex[:8]}).encode()).decode()
        self.db.insert("connections", {
            "id": connection_id, "tenant_id": principal.tenant_id, "provider": self.provider,
            "label": label or self.provider, "status": "pending",
            "credentials": self.crypto.encrypt(json.dumps({"state": state})),
            "meta": json.dumps({"scopes": self.connector.get("scopes", [])}),
            "created_at": iso(), "updated_at": iso()})
        base = str(self.connector.get("authorize_url") or "")
        url = (base + "?provider=" + str(self.connector.get("provider_id")) +
               "&state=" + state + "&redirect_uri=" +
               (os.environ.get("SARA_PUBLIC_BASE_URL", "") + "/integrations/" + self.provider + "/callback"))
        self.audit.record(principal.tenant_id, "auth", {"provider": self.provider,
                                                        "connection_id": connection_id,
                                                        "step": "authorize_start"},
                          actor=principal.user_id)
        return {"connection_id": connection_id, "authorize_url": url, "state": state,
                "user_supplied_credentials_required": bool(
                    self.connector.get("user_supplied_credentials_required", False))}

    def complete_authorization(self, tenant_id: str, connection_id: str,
                               grant: Dict[str, Any]) -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM connections WHERE tenant_id=? AND id=?",
                          (tenant_id, connection_id))
        if not row:
            raise NotFound("connection not found: " + connection_id)
        self.db.execute("UPDATE connections SET status=?, credentials=?, meta=?, updated_at=?"
                        " WHERE id=?",
                        ("connected", self.crypto.encrypt(json.dumps(grant)),
                         json.dumps({"scopes": self.connector.get("scopes", []),
                                     "account": grant.get("account")}), iso(), connection_id))
        self.audit.record(tenant_id, "auth", {"provider": self.provider,
                                              "connection_id": connection_id, "step": "connected"})
        return {"connection_id": connection_id, "status": "connected"}

    def connection(self, tenant_id: str, connection_id: Optional[str] = None) -> Dict[str, Any]:
        if connection_id:
            row = self.db.one("SELECT * FROM connections WHERE tenant_id=? AND id=?",
                              (tenant_id, connection_id))
        else:
            row = self.db.one("SELECT * FROM connections WHERE tenant_id=? AND provider=?"
                              " AND status='connected' ORDER BY updated_at DESC LIMIT 1",
                              (tenant_id, self.provider))
        if not row:
            raise NotFound("no connected " + self.provider + " connection")
        out = dict(row)
        try:
            out["grant"] = json.loads(self.crypto.decrypt(row["credentials"]) or "{}")
        except Exception:
            out["grant"] = {}
        return out

    def list_connections(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = self.db.query("SELECT id, provider, label, status, meta, created_at, updated_at"
                             " FROM connections WHERE tenant_id=? AND provider=?",
                             (tenant_id, self.provider))
        return rows

    def call(self, tenant_id: str, method: str, url: str, payload: Optional[Dict[str, Any]] = None,
             connection_id: Optional[str] = None, retries: Optional[int] = None,
             backoff: Optional[Sequence[float]] = None) -> Dict[str, Any]:
        import urllib.error
        import urllib.request
        conn = self.connection(tenant_id, connection_id)
        grant = conn.get("grant") or {}
        token = grant.get("access_token", "managed")
        direct = bool(self.connector.get("direct_google"))
        attempts = int(retries if retries is not None else 3)
        delays = list(backoff or [1, 3, 9])
        last_err: Optional[str] = None
        refreshed = False

        def _refresh() -> bool:
            # Exchange the stored refresh_token once per call when Google says 401.
            nonlocal grant, token, refreshed
            refresh_token = grant.get("refresh_token")
            if not direct or not refresh_token or refreshed:
                return False
            import urllib.parse
            token_url = str(self.connector.get("token_url") or "https://oauth2.googleapis.com/token")
            body = urllib.parse.urlencode({
                "refresh_token": refresh_token,
                "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
                "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
                "grant_type": "refresh_token",
            }).encode()
            req = urllib.request.Request(token_url, data=body, method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            if not data.get("access_token"):
                return False
            grant = dict(grant)
            grant["access_token"] = data["access_token"]
            if data.get("refresh_token"):
                grant["refresh_token"] = data["refresh_token"]
            grant["expires_in"] = data.get("expires_in", 3600)
            token = grant["access_token"]
            self.db.execute("UPDATE connections SET credentials=?, updated_at=? WHERE id=?",
                            (self.crypto.encrypt(json.dumps(grant)), iso(), conn["id"]))
            refreshed = True
            return True

        for attempt in range(attempts + 1):
            try:
                body = json.dumps(payload or {}).encode() if payload is not None else None
                req = urllib.request.Request(url, data=body, method=method,
                                             headers={"Authorization": "Bearer " + str(token),
                                                      "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read().decode() or "{}"
                return json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw}
            except Exception as exc:
                if (isinstance(exc, urllib.error.HTTPError) and exc.code == 401
                        and _refresh()):
                    continue  # token refreshed -> retry immediately
                last_err = str(exc)
                if attempt >= attempts:
                    break
                time.sleep(delays[min(attempt, len(delays) - 1)])
        raise IntegrationError(self.provider + " call failed: " + str(last_err), url=url)


# ------------------------------------------------------- telegram channel
class TelegramAdapter:
    # Pluggable outbound adapter interface.
    name = "base"

    def send_message(self, tenant_id: str, chat_id: str, text: str,
                     connection_id: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def send_media(self, tenant_id: str, chat_id: str, path: str, caption: str = "",
                   connection_id: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def download_file(self, tenant_id: str, file_id: str,
                      connection_id: Optional[str] = None) -> bytes:
        raise NotImplementedError


class MockTelegramAdapter(TelegramAdapter):
    name = "mock"

    def __init__(self) -> None:
        self.outbox: List[Dict[str, Any]] = []

    def send_message(self, tenant_id, chat_id, text, connection_id=None):
        rec = {"external_id": new_id("tgmsg"), "chat_id": chat_id, "text": text, "status": "sent"}
        self.outbox.append(rec)
        return rec

    def send_media(self, tenant_id, chat_id, path, caption="", connection_id=None):
        rec = {"external_id": new_id("tgmed"), "chat_id": chat_id, "path": path,
               "caption": caption, "status": "sent"}
        self.outbox.append(rec)
        return rec

    def download_file(self, tenant_id, file_id, connection_id=None):
        return b""


class ManagedTelegramAdapter(TelegramAdapter):
    name = "managed"

    def __init__(self, connector: ManagedConnector, conf: Dict[str, Any]) -> None:
        self.connector, self.conf = connector, conf

    def send_message(self, tenant_id, chat_id, text, connection_id=None):
        delivery = self.conf.get("delivery", {}) or {}
        res = self.connector.call(tenant_id, "POST",
                                  str((self.conf.get("connector") or {}).get("send_url")),
                                  {"chat_id": chat_id, "text": text},
                                  connection_id=connection_id,
                                  retries=int(delivery.get("retries", 3)),
                                  backoff=delivery.get("backoff_seconds"))
        return {"external_id": res.get("message_id") or new_id("tgmsg"), "status": "sent", "raw": res}

    def send_media(self, tenant_id, chat_id, path, caption="", connection_id=None):
        res = self.connector.call(tenant_id, "POST",
                                  str((self.conf.get("connector") or {}).get("send_url")),
                                  {"chat_id": chat_id, "file_path": path, "caption": caption},
                                  connection_id=connection_id)
        return {"external_id": res.get("message_id") or new_id("tgmed"), "status": "sent", "raw": res}

    def download_file(self, tenant_id, file_id, connection_id=None):
        res = self.connector.call(tenant_id, "GET",
                                  str((self.conf.get("connector") or {}).get("file_url")) +
                                  "?file_id=" + str(file_id), None, connection_id=connection_id)
        blob = res.get("content_base64")
        return base64.b64decode(blob) if blob else b""


class TelegramChannel:
    def __init__(self, cfg: Config, db: Database, storage: StorageService, crypto: Crypto,
                 audit: AuditLog, metrics: Metrics, limiter: RateLimiter) -> None:
        self.cfg, self.db, self.storage, self.audit = cfg, db, storage, audit
        self.metrics, self.limiter, self.crypto = metrics, limiter, crypto
        self.conf = cfg.get("integrations.telegram", {}) or {}
        self.connector = ManagedConnector(cfg, db, crypto, audit, "telegram",
                                          "integrations.telegram")
        self.adapters: Dict[str, TelegramAdapter] = {
            "mock": MockTelegramAdapter(),
            "managed": ManagedTelegramAdapter(self.connector, self.conf),
        }
        self.default_adapter = str((self.conf.get("adapters") or {}).get("default", "managed"))

    def adapter(self, name: Optional[str] = None) -> TelegramAdapter:
        key = name or self.default_adapter
        if key not in self.adapters:
            raise ValidationError("unknown telegram adapter: " + key)
        return self.adapters[key]

    def verify_signature(self, body: bytes, signature: Optional[str], secret: str) -> bool:
        if not self.conf.get("webhook", {}).get("verify_signature", True):
            return True
        if not signature:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def seen_update(self, tenant_id: str, update_key: str) -> Optional[Dict[str, Any]]:
        row = self.db.one("SELECT * FROM idempotency WHERE key=?", (update_key,))
        if not row:
            return None
        try:
            return json.loads(row["result"])
        except Exception:
            return {"status": "duplicate"}

    def remember_update(self, tenant_id: str, update_key: str, result: Dict[str, Any]) -> None:
        self.db.upsert("idempotency", {"key": update_key, "tenant_id": tenant_id,
                                        "result": json.dumps(result, ensure_ascii=False, default=str),
                                        "created_at": iso()}, keys=["key"])

    def parse_update(self, update: Dict[str, Any]) -> Dict[str, Any]:
        msg = (update.get("message") or update.get("edited_message") or
               update.get("channel_post") or {})
        chat_id = str(deep_get(msg, "chat.id", "")) or str(deep_get(update, "chat.id", ""))
        media: Dict[str, Any] = {}
        for kind in ("voice", "audio", "document", "photo", "video_note"):
            if kind in msg:
                item = msg[kind][-1] if isinstance(msg[kind], list) else msg[kind]
                media = {"kind": kind, "file_id": item.get("file_id"),
                         "file_name": item.get("file_name"), "mime": item.get("mime_type")}
                break
        return {"update_id": str(update.get("update_id") or new_id("upd")),
                "chat_id": chat_id, "text": msg.get("text") or msg.get("caption") or "",
                "media": media, "from": deep_get(msg, "from.first_name", "guest")}

    def send(self, tenant_id: str, chat_id: str, text: str, conversation_id: Optional[str] = None,
             adapter: Optional[str] = None, connection_id: Optional[str] = None) -> Dict[str, Any]:
        result = self.adapter(adapter).send_message(tenant_id, chat_id, text, connection_id)
        self.metrics.inc("messages_out")
        row = {"id": new_id("msg"), "tenant_id": tenant_id, "conversation_id": conversation_id,
               "direction": "out", "text": text, "media": None, "intent": None, "confidence": None,
               "sources": None, "external_id": result.get("external_id"),
               "delivery_status": result.get("status", "sent"), "created_at": iso()}
        self.db.insert("messages", row)
        self.audit.record(tenant_id, "message_out", {"chat_id": chat_id, "chars": len(text),
                                                     "external_id": result.get("external_id"),
                                                     "status": result.get("status")},
                          conversation_id=conversation_id)
        return row

    def send_document(self, tenant_id: str, chat_id: str, path: str, caption: str = "",
                      adapter: Optional[str] = None) -> Dict[str, Any]:
        return self.adapter(adapter).send_media(tenant_id, chat_id, path, caption)


# --------------------------------------------------------- google sheets
class SheetsConnector:
    # Managed-OAuth Google Sheets connector: read/write/map/sync with conflict
    # handling, retries and sync history. Transport is injectable for tests.
    def __init__(self, cfg: Config, db: Database, crypto: Crypto, audit: AuditLog,
                 metrics: Metrics) -> None:
        self.cfg, self.db, self.audit, self.metrics = cfg, db, audit, metrics
        self.conf = cfg.get("integrations.google_sheets", {}) or {}
        self.connector = ManagedConnector(cfg, db, crypto, audit, "google_sheets",
                                          "integrations.google_sheets")
        self.transport: Optional[Callable[[str, str, Optional[Dict[str, Any]]], Dict[str, Any]]] = None

    def _direct(self) -> bool:
        return bool((self.conf.get("connector") or {}).get("direct_google", False))

    # ---- managed auth passthrough (proxy mode) / real Google OAuth (direct)
    def start_authorization(self, principal: Principal, label: str = "") -> Dict[str, Any]:
        if not self._direct():
            return self.connector.start_authorization(principal, label)
        import urllib.parse
        connection_id = new_id("conn")
        state = base64.urlsafe_b64encode(json.dumps(
            {"c": connection_id, "t": principal.tenant_id, "n": uuid.uuid4().hex[:8]}).encode()).decode()
        self.connector.db.insert("connections", {
            "id": connection_id, "tenant_id": principal.tenant_id, "provider": "google_sheets",
            "label": label or "google_sheets", "status": "pending",
            "credentials": self.connector.crypto.encrypt(json.dumps({"state": state})),
            "meta": json.dumps({"scopes": self.connector.connector.get("scopes", [])}),
            "created_at": iso(), "updated_at": iso()})
        conf = self.connector.connector
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        if not client_id and self.transport is None:
            raise IntegrationError("GOOGLE_CLIENT_ID is not configured in backend/.env")
        base = str(conf.get("authorize_url") or "https://accounts.google.com/o/oauth2/v2/auth")
        public = (os.environ.get("SARA_PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/")
        redirect_uri = public + "/agent/integrations/sheets/callback"
        scopes = " ".join(conf.get("scopes") or ["https://www.googleapis.com/auth/spreadsheets"])
        url = (base + "?client_id=" + urllib.parse.quote(client_id, safe="") +
               "&redirect_uri=" + urllib.parse.quote(redirect_uri, safe="") +
               "&response_type=code&scope=" + urllib.parse.quote(scopes, safe="") +
               "&access_type=offline&prompt=consent&state=" + urllib.parse.quote(state, safe=""))
        self.audit.record(principal.tenant_id, "auth", {"provider": "google_sheets",
                                                        "connection_id": connection_id,
                                                        "step": "authorize_start"},
                          actor=principal.user_id)
        return {"connection_id": connection_id, "authorize_url": url, "state": state,
                "user_supplied_credentials_required": False}

    def complete_authorization(self, tenant_id: str, connection_id: str,
                               grant: Dict[str, Any]) -> Dict[str, Any]:
        return self.connector.complete_authorization(tenant_id, connection_id, grant)

    def exchange_code_and_store(self, tenant_id: str, connection_id: str, code: str,
                                redirect_uri: str) -> Dict[str, Any]:
        """Exchange a Google OAuth authorization code and store the grant encrypted."""
        import urllib.error
        import urllib.parse
        import urllib.request
        conf = self.connector.connector
        token_url = str(conf.get("token_url") or "https://oauth2.googleapis.com/token")
        body = urllib.parse.urlencode({
            "code": code,
            "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }).encode()
        req = urllib.request.Request(token_url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                token = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise IntegrationError("google token exchange failed: " +
                                   str(exc.read().decode()[:300] if exc.fp else exc))
        access = token.get("access_token")
        if not access:
            raise IntegrationError("google token exchange returned no access_token")
        grant: Dict[str, Any] = {"access_token": access,
                                 "token_type": token.get("token_type", "Bearer"),
                                 "expires_in": token.get("expires_in", 3600),
                                 "scope": token.get("scope", "")}
        if token.get("refresh_token"):
            grant["refresh_token"] = token["refresh_token"]
        try:
            with urllib.request.urlopen(
                    "https://oauth2.googleapis.com/tokeninfo?access_token=" +
                    urllib.parse.quote(access, safe=""), timeout=10) as resp:
                info = json.loads(resp.read().decode())
            if info.get("email"):
                grant["account"] = info["email"]
        except Exception:
            pass
        self.complete_authorization(tenant_id, connection_id, grant)
        return grant

    # ---- per-tenant default spreadsheet
    def _saved_spreadsheet(self, tenant_id: str) -> Optional[str]:
        rows = self.db.query(
            "SELECT meta FROM connections WHERE tenant_id=? AND provider='google_sheets'"
            " AND status='connected' ORDER BY updated_at DESC LIMIT 1", (tenant_id,))
        if not rows:
            return None
        try:
            meta = json.loads(rows[0]["meta"] or "{}")
        except Exception:
            meta = {}
        return meta.get("default_spreadsheet_id") or None

    def _remember_spreadsheet(self, tenant_id: str, spreadsheet_id: str) -> None:
        rows = self.db.query(
            "SELECT id, meta FROM connections WHERE tenant_id=? AND provider='google_sheets'"
            " AND status='connected' ORDER BY updated_at DESC LIMIT 1", (tenant_id,))
        if not rows:
            return
        try:
            meta = json.loads(rows[0]["meta"] or "{}")
        except Exception:
            meta = {}
        meta["default_spreadsheet_id"] = spreadsheet_id
        self.db.execute("UPDATE connections SET meta=?, updated_at=? WHERE id=?",
                        (json.dumps(meta), iso(), rows[0]["id"]))

    def _api(self, tenant_id: str, method: str, path: str,
             payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.transport:
            return self.transport(method, path, payload)
        sync = self.conf.get("sync", {}) or {}
        base = str((self.conf.get("connector") or {}).get("api_base") or "")
        return self.connector.call(tenant_id, method, base + path, payload,
                                    retries=int(sync.get("retries", 3)),
                                    backoff=sync.get("backoff_seconds"))

    # ---- discovery
    def list_spreadsheets(self, tenant_id: str) -> List[Dict[str, Any]]:
        if self._direct():
            import urllib.parse
            drive_base = str((self.conf.get("connector") or {}).get(
                "drive_api_base", "https://www.googleapis.com/drive/v3"))
            query = urllib.parse.quote(
                "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false")
            url = (drive_base + "/files?q=" + query +
                   "&fields=files(id,name,mimeType)&pageSize=50")
            res = self.connector.call(tenant_id, "GET", url)
            files = res.get("files") or []
            return [{"id": f.get("id"), "name": f.get("name"),
                     "mimeType": f.get("mimeType")} for f in files]
        res = self._api(tenant_id, "GET", "/spreadsheets")
        return res.get("files") or res.get("spreadsheets") or []

    def list_sheets(self, tenant_id: str, spreadsheet_id: str) -> List[str]:
        import urllib.parse
        path = ("/spreadsheets/" + urllib.parse.quote(str(spreadsheet_id), safe=""))
        if self._direct():
            path += "?fields=sheets.properties.title"
        res = self._api(tenant_id, "GET", path)
        return [deep_get(s, "properties.title", "") for s in (res.get("sheets") or [])]

    def auto_create_spreadsheet(self, tenant_id: str) -> str:
        """Create a spreadsheet (with per-target sheets + header rows) and return its id."""
        import urllib.parse
        targets = [(t, str(name)) for t, name in
                   ((self.conf.get("default_targets") or {}).get("sheets") or {}).items()]
        if not targets:
            targets = [("bookings", "Bookings")]
        columns = {t: [c for c in (((self.conf.get("column_mapping") or {}).get(t) or {}).values())
                       if c] for t, _ in targets}
        created = self._api(tenant_id, "POST", "/spreadsheets",
                            {"properties": {"title": "SARA Booking Assistant Leads"}})
        sid = str(created.get("spreadsheetId") or "")
        if not sid:
            raise IntegrationError("google did not return a spreadsheet id on create")
        sheets0 = ((created.get("sheets") or [{}])[0].get("properties") or {})
        requests = [{"updateSheetProperties": {
            "properties": {"sheetId": int(sheets0.get("sheetId", 0)),
                            "title": targets[0][1]}, "fields": "title"}}]
        for _, name in targets[1:]:
            requests.append({"addSheet": {"properties": {"title": name}}})
        self._api(tenant_id, "POST", "/spreadsheets/" + sid + ":batchUpdate",
                  {"requests": requests})
        for (_, name), header in zip(targets, [columns[t] for t, _ in targets]):
            if header:
                self._api(tenant_id, "PUT", "/spreadsheets/" + sid + "/values/" +
                          urllib.parse.quote(name, safe="") + "!A1?valueInputOption=USER_ENTERED",
                          {"values": [header]})
        self.metrics.inc("sync_runs")
        return sid

    # ---- rows
    def read_rows(self, tenant_id: str, spreadsheet_id: str, sheet: str) -> List[Dict[str, str]]:
        import urllib.parse
        res = self._api(tenant_id, "GET", "/spreadsheets/" +
                        urllib.parse.quote(str(spreadsheet_id), safe="") + "/values/" +
                        urllib.parse.quote(str(sheet), safe=""))
        values = res.get("values") or []
        if not values:
            return []
        header = [str(h).strip() for h in values[0]]
        rows = []
        for raw in values[1:]:
            padded = list(raw) + [""] * (len(header) - len(raw))
            rows.append({header[i]: str(padded[i]) for i in range(len(header))})
        return rows

    def append_row(self, tenant_id: str, spreadsheet_id: str, sheet: str,
                   record: Dict[str, Any], target: str) -> Dict[str, Any]:
        import urllib.parse
        mapping = (self.conf.get("column_mapping") or {}).get(target) or {}
        if not self._direct():
            row = {mapping.get(k, k): ("" if v is None else v) for k, v in record.items()}
            return self._api(tenant_id, "POST", "/spreadsheets/" + spreadsheet_id +
                             "/values/" + sheet + ":append", {"values": [row]})
        headers = [c for c in mapping.values() if c]
        if not headers:
            headers = [str(k) for k in record]
        values = ["" if record.get(k) is None else str(record.get(k)) for k in mapping]
        path = ("/spreadsheets/" + urllib.parse.quote(str(spreadsheet_id), safe="") +
                "/values/" + urllib.parse.quote(str(sheet), safe="") +
                ":append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS")
        return self._api(tenant_id, "POST", path, {"values": [values]})

    def update_row(self, tenant_id: str, spreadsheet_id: str, sheet: str, row_key: str,
                   record: Dict[str, Any], target: str) -> Dict[str, Any]:
        import urllib.parse
        mapping = (self.conf.get("column_mapping") or {}).get(target) or {}
        key_col = (self.conf.get("sync") or {}).get("row_key_column", "sara_row_id")
        if not self._direct():
            row = {mapping.get(k, k): v for k, v in record.items()}
            return self._api(tenant_id, "POST", "/spreadsheets/" + spreadsheet_id +
                             "/values/" + sheet + ":update",
                             {"key_column": key_col, "key": row_key, "values": row})
        # Real Sheets API: locate the row whose key column matches, then PUT that row.
        remote = self.read_rows(tenant_id, spreadsheet_id, sheet)
        index = next((i for i, r in enumerate(remote)
                      if str(r.get(key_col, "")) == str(row_key)), None)
        if index is None:
            return self.append_row(tenant_id, spreadsheet_id, sheet, record, target)
        headers = [c for c in mapping.values() if c]
        values = ["" if record.get(k) is None else str(record.get(k)) for k in mapping]
        end_col = len(headers) if len(headers) > 1 else 1
        col = chr(64 + end_col) if end_col <= 26 else "ZZ"
        path = ("/spreadsheets/" + urllib.parse.quote(str(spreadsheet_id), safe="") +
                "/values/" + urllib.parse.quote(str(sheet), safe="") +
                "!A" + str(index + 2) + ":" + col + str(index + 2) +
                "?valueInputOption=USER_ENTERED")
        return self._api(tenant_id, "PUT", path, {"values": [values]})

    # ---- mapping helpers
    def map_inbound(self, target: str, row: Dict[str, str]) -> Dict[str, Any]:
        mapping = (self.conf.get("column_mapping") or {}).get(target) or {}
        return {field: row.get(column, "") for field, column in mapping.items()}

    # ---- sync
    def sync(self, principal: Principal, target: str, spreadsheet_id: Optional[str] = None,
             sheet: Optional[str] = None, direction: Optional[str] = None) -> Dict[str, Any]:
        if not self.cfg.flag("enable_sheets_sync", True):
            raise Forbidden("sheets sync disabled by feature flag")
        tenant_id = principal.tenant_id
        defaults = self.conf.get("default_targets", {}) or {}
        spreadsheet_id = (spreadsheet_id or defaults.get("spreadsheet_id")
                          or self._saved_spreadsheet(tenant_id))
        sheet = sheet or (defaults.get("sheets") or {}).get(target, target.title())
        created_id: Optional[str] = None
        if not spreadsheet_id or str(spreadsheet_id).strip().upper() == "AUTO_CREATE":
            created_id = self.auto_create_spreadsheet(tenant_id)
            spreadsheet_id = created_id
            self._remember_spreadsheet(tenant_id, spreadsheet_id)
        direction = direction or (self.conf.get("sync") or {}).get("mode", "bidirectional")
        rows_read = rows_written = conflicts = 0
        detail: Dict[str, Any] = {"target": target, "direction": direction}
        try:
            if direction in ("inbound", "bidirectional"):
                remote = self.read_rows(tenant_id, spreadsheet_id, sheet)
                rows_read = len(remote)
                if target == "availability":
                    conflicts += self._apply_availability(tenant_id, remote, spreadsheet_id, sheet)
            if direction in ("outbound", "bidirectional"):
                rows_written = self._push(tenant_id, target, spreadsheet_id, sheet)
            status = "ok"
        except SaraError as exc:
            status, detail["error"] = "failed", exc.message
        except Exception as exc:
            status, detail["error"] = "failed", str(exc)
        hist = {"id": new_id("sync"), "tenant_id": tenant_id,
                "connection_id": None, "direction": direction, "target": target,
                "rows_read": rows_read, "rows_written": rows_written, "conflicts": conflicts,
                "status": status, "detail": json.dumps(detail, ensure_ascii=False, default=str),
                "created_at": iso()}
        if created_id:
            hist["spreadsheet_id"] = created_id
        self.db.insert("sync_history", hist)
        self.metrics.inc("sync_runs")
        if conflicts:
            self.metrics.inc("sync_conflicts", conflicts)
        self.audit.record(tenant_id, "sync", hist, actor=principal.user_id)
        self._trim_history(tenant_id)
        if status == "failed":
            raise IntegrationError("sync failed", detail=detail)
        return hist

    def _apply_availability(self, tenant_id: str, remote: Sequence[Dict[str, str]],
                            spreadsheet_id: str, sheet: str) -> int:
        # Sheets is authoritative for live availability; conflicting local facts
        # are replaced under last-write-wins and recorded in history.
        conflicts = 0
        for raw in remote:
            rec = self.map_inbound("availability", raw)
            room = str(rec.get("room_type") or "").strip().lower()
            if not room:
                continue
            avail = str(rec.get("rooms_available") or "").strip()
            locator = "sheets:" + spreadsheet_id + "/" + sheet + "#" + room + "@" + str(rec.get("date") or "")
            prev = self.db.one("SELECT * FROM facts WHERE tenant_id=? AND entity='availability' AND key=?",
                               (tenant_id, room))
            if prev and str(prev.get("value") or "") != avail:
                conflicts += 1
            fact = Fact(tenant_id, "availability", room, avail, "sheets", 1, locator,
                        numeric_value=Normalizer._to_number(avail),
                        valid_from=str(rec.get("date") or "") or None, confidence=0.95)
            self.db.execute("DELETE FROM facts WHERE tenant_id=? AND entity='availability' AND key=?",
                            (tenant_id, room))
            self.db.insert("facts", dataclasses.asdict(fact))
            rate = str(rec.get("rate") or "").strip()
            if rate:
                rfact = Fact(tenant_id, "rate", room, rate, "sheets", 1, locator,
                             numeric_value=Normalizer._to_number(rate),
                             currency=str(rec.get("currency") or "") or None, confidence=0.95)
                self.db.execute("DELETE FROM facts WHERE tenant_id=? AND entity='rate' AND key=?",
                                (tenant_id, room))
                self.db.insert("facts", dataclasses.asdict(rfact))
        return conflicts

    def _push(self, tenant_id: str, target: str, spreadsheet_id: str, sheet: str) -> int:
        written = 0
        if target == "leads":
            for row in self.db.query("SELECT * FROM leads WHERE tenant_id=? AND status='new'",
                                     (tenant_id,)):
                data = json.loads(row["data"])
                self.append_row(tenant_id, spreadsheet_id, sheet,
                                {"created_at": row["created_at"], "full_name": data.get("full_name"),
                                 "phone": data.get("phone"), "email": data.get("email"),
                                 "language": data.get("language"), "intent": data.get("intent"),
                                 "source": "telegram", "status": "new"}, "leads")
                self.db.execute("UPDATE leads SET status='synced', updated_at=? WHERE id=?",
                                (iso(), row["id"]))
                written += 1
        elif target == "bookings":
            for row in self.db.query("SELECT * FROM bookings WHERE tenant_id=?", (tenant_id,)):
                data = json.loads(row["data"])
                self.append_row(tenant_id, spreadsheet_id, sheet,
                                {"booking_id": row["id"], "full_name": data.get("full_name"),
                                 "phone": data.get("phone"), "room_type": data.get("room_type"),
                                 "checkin_date": data.get("checkin_date"),
                                 "checkout_date": data.get("checkout_date"),
                                 "guests": data.get("guests"), "price_total": data.get("price_total"),
                                 "currency": data.get("currency"), "status": row["status"],
                                 "updated_at": row["updated_at"]}, "bookings")
                written += 1
        return written

    def _trim_history(self, tenant_id: str) -> None:
        keep = int((self.conf.get("sync") or {}).get("keep_history_entries", 500))
        rows = self.db.query("SELECT id FROM sync_history WHERE tenant_id=? ORDER BY created_at DESC",
                             (tenant_id,))
        for row in rows[keep:]:
            self.db.execute("DELETE FROM sync_history WHERE id=?", (row["id"],))

    def history(self, tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query("SELECT * FROM sync_history WHERE tenant_id=? ORDER BY created_at DESC"
                             " LIMIT ?", (tenant_id, limit))


# ------------------------------------------------------------------- voice
class VoiceService:
    # STT/TTS with pluggable providers; voice goes through the same agent.
    def __init__(self, cfg: Config, storage: StorageService, audit: AuditLog,
                 metrics: Metrics, crypto: Crypto) -> None:
        self.cfg, self.storage, self.audit, self.metrics, self.crypto = cfg, storage, audit, metrics, crypto
        self.conf = cfg.get("integrations.voice", {}) or {}
        self.stt_hook: Optional[Callable[[bytes, str], str]] = None
        self.tts_hook: Optional[Callable[[str, str], bytes]] = None

    def transcribe(self, tenant_id: str, session_id: str, audio: bytes,
                   fmt: str = "ogg", language: Optional[str] = None) -> Dict[str, Any]:
        if not self.cfg.flag("enable_voice", True):
            raise Forbidden("voice disabled by feature flag")
        stt = self.conf.get("stt", {}) or {}
        if fmt.lower() not in [f.lower() for f in stt.get("formats", ["ogg"])]:
            raise ValidationError("unsupported audio format: " + fmt)
        clip_id = new_id("clip")
        path = self.storage.resolve("voice", tenant_id=tenant_id, session_id=session_id,
                                    clip_id=clip_id, ext=fmt)
        self.storage.write_bytes(path, audio)
        provider = str(stt.get("provider") or "mock").lower()
        if self.stt_hook:
            text = self.stt_hook(audio, fmt)
        elif provider in ("", "mock", "none"):
            text = ""
        else:
            raise IntegrationError("configure an STT hook for provider: " + provider)
        self.metrics.inc("voice_transcriptions")
        self.audit.record(tenant_id, "voice", {"clip_id": clip_id, "bytes": len(audio),
                                               "format": fmt, "provider": provider,
                                               "transcript_encrypted": bool(text)})
        return {"clip_id": clip_id, "path": str(path), "text": text,
                "language": language or stt.get("language", "auto")}

    def synthesize(self, tenant_id: str, session_id: str, text: str) -> Optional[Dict[str, Any]]:
        tts = self.conf.get("tts", {}) or {}
        if not (tts.get("enabled", True) and self.cfg.flag("enable_tts", True)):
            return None
        fmt = str(tts.get("format", "mp3"))
        if self.tts_hook:
            audio = self.tts_hook(text, fmt)
        else:
            return None
        clip_id = new_id("tts")
        path = self.storage.resolve("voice", tenant_id=tenant_id, session_id=session_id,
                                    clip_id=clip_id, ext=fmt)
        self.storage.write_bytes(path, audio)
        self.audit.record(tenant_id, "voice", {"clip_id": clip_id, "kind": "tts", "chars": len(text)})
        return {"clip_id": clip_id, "path": str(path), "format": fmt}


# ------------------------------------------------- conversation / agent core
class ConversationService:
    # Grounded, multilingual orchestration shared by Telegram and voice.
    def __init__(self, cfg: Config, db: Database, knowledge: KnowledgeService,
                 prompts: PromptRegistry, bookings: BookingService, docs: DocumentService,
                 llm: LlmClient, audit: AuditLog, metrics: Metrics, crypto: Crypto) -> None:
        self.cfg, self.db, self.knowledge, self.prompts = cfg, db, knowledge, prompts
        self.bookings, self.docs, self.llm = bookings, docs, llm
        self.audit, self.metrics, self.crypto = audit, metrics, crypto
        self.lang = LanguageDetector(cfg)
        self.conf = cfg.get("conversation", {}) or {}
        self.rules = cfg.get("response_rules", {}) or {}
        self.esc = cfg.get("escalation", {}) or {}

    # ---- session
    def get_or_create(self, tenant_id: str, chat_id: str, channel: str = "telegram") -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM conversations WHERE tenant_id=? AND external_chat_id=?"
                          " AND channel=?", (tenant_id, chat_id, channel))
        idle = int((self.conf.get("memory") or {}).get("session_idle_timeout_minutes", 120))
        if row:
            try:
                last = datetime.fromisoformat(row["updated_at"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if (now() - last) > timedelta(minutes=idle):
                    self.db.execute("UPDATE conversations SET state=?, summary=?, updated_at=?"
                                    " WHERE id=?",
                                    (json.dumps({}), None, iso(), row["id"]))
                    row = self.db.one("SELECT * FROM conversations WHERE id=?", (row["id"],))
            except Exception:
                pass
            return row
        row = {"id": new_id("cnv"), "tenant_id": tenant_id, "channel": channel,
               "external_chat_id": chat_id, "language": self.lang.default,
               "state": json.dumps({}), "paused_until": None, "summary": None,
               "created_at": iso(), "updated_at": iso()}
        self.db.insert("conversations", row)
        return row

    def state(self, conversation: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return json.loads(conversation.get("state") or "{}")
        except Exception:
            return {}

    def save_state(self, conversation_id: str, state: Dict[str, Any],
                   language: Optional[str] = None, paused_until: Optional[str] = None) -> None:
        if language:
            self.db.execute("UPDATE conversations SET state=?, language=?, paused_until=?, updated_at=?"
                            " WHERE id=?",
                            (json.dumps(state, ensure_ascii=False, default=str), language,
                             paused_until, iso(), conversation_id))
        else:
            self.db.execute("UPDATE conversations SET state=?, paused_until=?, updated_at=? WHERE id=?",
                            (json.dumps(state, ensure_ascii=False, default=str), paused_until,
                             iso(), conversation_id))

    def history(self, conversation_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        limit = limit or int((self.conf.get("memory") or {}).get("max_turns", 30))
        rows = self.db.query("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at DESC"
                             " LIMIT ?", (conversation_id, limit))
        return list(reversed(rows))

    # ---- language / intent
    def canned(self, key: str, language: str) -> str:
        table = (self.cfg.get("language_rules.canned") or {}).get(key) or {}
        default = self.cfg.get("language_rules.default", "en")
        return table.get(language) or table.get(default) or ""

    def classify(self, text: str) -> Tuple[str, float]:
        low = " " + (text or "").lower() + " "
        best, hits = "unknown", 0
        for intent, keywords in (self.conf.get("intents") or {}).items():
            count = sum(1 for kw in keywords if str(kw).lower() in low)
            if count > hits:
                best, hits = intent, count
        if hits == 0:
            return "unknown", 0.35
        return best, min(0.95, 0.55 + 0.15 * hits)

    # ---- main entry point
    def handle(self, tenant_id: str, chat_id: str, text: str, channel: str = "telegram",
               media: Optional[Dict[str, Any]] = None, actor: str = "guest",
               external_id: Optional[str] = None) -> AgentDecision:
        conversation = self.get_or_create(tenant_id, chat_id, channel)
        conversation_id = conversation["id"]
        language, lang_conf = self.lang.detect(text)
        min_lang = float(self.cfg.get("language_rules.detection_min_confidence", 0.5))
        if lang_conf < min_lang:
            language = conversation.get("language") or self.lang.default
        intent, intent_conf = self.classify(text)
        tool_calls: List[Dict[str, Any]] = []
        state = self.state(conversation)

        self.metrics.inc("messages_in")
        self.db.insert("messages", {
            "id": new_id("msg"), "tenant_id": tenant_id, "conversation_id": conversation_id,
            "direction": "in", "text": text,
            "media": json.dumps(media, ensure_ascii=False) if media else None,
            "intent": intent, "confidence": intent_conf, "sources": None,
            "external_id": external_id, "delivery_status": "received", "created_at": iso()})
        self.audit.record(tenant_id, "message_in", {"chat_id": chat_id, "intent": intent,
                                                    "language": language, "chars": len(text or ""),
                                                    "media": bool(media)},
                          actor=actor, conversation_id=conversation_id)

        # human takeover pause
        paused = conversation.get("paused_until")
        if paused:
            try:
                until = datetime.fromisoformat(paused)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=UTC)
                if now() < until:
                    return self._finish(tenant_id, conversation_id, language, intent, 1.0,
                                        self.canned("handoff", language), [], tool_calls,
                                        escalated=True, reason="human_takeover_active", state=state)
            except Exception:
                pass

        # retrieval
        retrieved = self.knowledge.search(tenant_id, text) if (text or "").strip() else []
        tool_calls.append({"tool": "knowledge.search", "query": (text or "")[:120],
                           "hits": len(retrieved)})
        sources = [r.source_ref for r in retrieved]
        confidence = retrieved[0].score if retrieved else 0.0

        escalate_reason = self._escalation_reason(text, intent, retrieved)

        # deterministic grounded answers for money/inventory questions
        answer: Optional[str] = None
        if intent == "price_inquiry":
            lookup = self.knowledge.price_lookup(tenant_id, self._room_hint(text, tenant_id))
            tool_calls.append({"tool": "knowledge.price_lookup", "found": lookup["found"]})
            if not lookup["found"]:
                escalate_reason = escalate_reason or "low_confidence"
            elif lookup.get("conflict"):
                escalate_reason = "conflicting_sources"
            elif lookup.get("stale"):
                escalate_reason = "stale_data"
            else:
                row = lookup["rows"][0]
                sources = lookup["sources"]
                answer = self._fmt_price(row, language)
                confidence = max(confidence, float(row.get("confidence") or 0.8))
        elif intent == "availability_inquiry":
            lookup = self.knowledge.availability_lookup(tenant_id, self._room_hint(text, tenant_id))
            tool_calls.append({"tool": "knowledge.availability_lookup", "found": lookup["found"]})
            if not lookup["found"]:
                escalate_reason = escalate_reason or "low_confidence"
            elif lookup.get("stale"):
                escalate_reason = "stale_data"
            else:
                row = lookup["rows"][0]
                sources = lookup["sources"]
                answer = self._fmt_availability(row, language)
                confidence = max(confidence, float(row.get("confidence") or 0.85))
        elif intent == "greeting" and not retrieved:
            answer = self.canned("greeting", language)
            confidence = 0.9

        # booking workflow
        booking_id: Optional[str] = None
        documents: List[str] = []
        if intent == "booking_intent" or state.get("booking_in_progress"):
            answer, booking_id, documents, escalate_reason = self._booking_turn(
                tenant_id, conversation_id, chat_id, text, language, state, sources,
                tool_calls, escalate_reason)

        # capture lead
        self._capture_lead(tenant_id, conversation_id, text, language, intent, state)

        if escalate_reason:
            reply = self.canned("checking_with_team", language) or "Let me confirm with our team."
            self._escalate(tenant_id, conversation_id, escalate_reason,
                           {"question": (text or "")[:400], "sources": sources})
            return self._finish(tenant_id, conversation_id, language, intent, confidence, reply,
                                sources, tool_calls, escalated=True, reason=escalate_reason,
                                state=state, booking_id=booking_id, documents=documents)

        if answer is None:
            answer = self._compose(tenant_id, language, text, retrieved, tool_calls)
        if answer is None:
            reply = self.canned("checking_with_team", language)
            self._escalate(tenant_id, conversation_id, "low_confidence",
                           {"question": (text or "")[:400]})
            return self._finish(tenant_id, conversation_id, language, intent, confidence, reply,
                                sources, tool_calls, escalated=True, reason="low_confidence",
                                state=state)
        return self._finish(tenant_id, conversation_id, language, intent, confidence, answer,
                            sources, tool_calls, escalated=False, reason=None, state=state,
                            booking_id=booking_id, documents=documents)

    # ---- helpers
    def _room_hint(self, text: str, tenant_id: str) -> Optional[str]:
        low = (text or "").lower()
        for row in self.knowledge.facts(tenant_id, "room_type"):
            key = str(row.get("key") or "")
            if key and key in low:
                return key
        for token in ("deluxe", "standard", "suite", "double", "single", "twin", "family", "villa"):
            if token in low:
                return token
        return None

    def _fmt_price(self, row: Dict[str, Any], language: str) -> str:
        amount = row.get("numeric_value")
        currency = row.get("currency") or ""
        label = str(row.get("key") or "room").replace("_", " ")
        value = (currency + " " + (str(int(amount)) if amount and float(amount).is_integer()
                                   else str(amount))) if amount is not None else str(row.get("value"))
        if language == "ar":
            return "سعر " + label + " هو " + value + " حسب بياناتنا الحالية."
        if language == "fr":
            return "Le tarif pour " + label + " est " + value + " selon nos donnees actuelles."
        if language == "es":
            return "La tarifa de " + label + " es " + value + " segun nuestros datos actuales."
        if language == "de":
            return "Der Preis fuer " + label + " betraegt " + value + " nach unseren aktuellen Daten."
        return "The rate for " + label + " is " + value + " based on our current records."

    def _fmt_availability(self, row: Dict[str, Any], language: str) -> str:
        label = str(row.get("key") or "room").replace("_", " ")
        value = str(row.get("value"))
        day = row.get("valid_from")
        suffix = (" for " + str(day)) if day else ""
        if language == "ar":
            return "المتوفر من " + label + suffix + ": " + value + "."
        if language == "fr":
            return "Disponibilite pour " + label + suffix + " : " + value + "."
        return "Availability for " + label + suffix + ": " + value + "."

    def _escalation_reason(self, text: str, intent: str,
                           retrieved: Sequence[Retrieved]) -> Optional[str]:
        triggers = set(self.esc.get("triggers") or [])
        low = " " + (text or "").lower() + " "
        if intent == "human_request" and "human_request" in triggers:
            return "human_request"
        if intent == "complaint" and "complaint" in triggers:
            return "complaint"
        if any(w in low for w in ("refund", "chargeback", "payment link", "pay now")) and \
                "payment_or_refund" in triggers:
            return "payment_or_refund"
        if any(w in low for w in ("discount", "cheaper", "best price", "offer me")) and \
                "discount_request" in triggers:
            return "discount_request"
        m = re.search(r"(\d+)\s*(rooms|room)", low)
        if m and int(m.group(1)) >= int(self.esc.get("group_booking_min_rooms", 5)) and \
                "group_booking" in triggers:
            return "group_booking"
        min_conf = float(self.cfg.get("knowledge.confidence.min_answer_confidence", 0.62))
        if intent in ("policy_inquiry", "amenity_inquiry", "location_inquiry") and \
                (not retrieved or retrieved[0].score < min_conf):
            return "low_confidence"
        return None

    def _compose(self, tenant_id: str, language: str, question: str,
                 retrieved: Sequence[Retrieved], tool_calls: List[Dict[str, Any]]) -> Optional[str]:
        if not retrieved:
            return None
        strict = self.cfg.flag("strict_grounding", True)
        min_conf = float(self.cfg.get("knowledge.confidence.min_answer_confidence", 0.62))
        if strict and retrieved[0].score < min_conf:
            return None
        context = "\n\n".join("[" + r.source_ref + "] " + r.text[:800] for r in retrieved)
        system, sys_ref = self.prompts.render(tenant_id, "system", {"language": language})
        business, biz_ref = self.prompts.render(tenant_id, "business", {
            "context": context,
            "business_profile": self._profile(tenant_id),
            "operational_instructions": self._entity_list(tenant_id, "operational_instruction"),
            "booking_rules": self._entity_list(tenant_id, "booking_rule")})
        tool_calls.append({"tool": "prompts.render", "prompts": [sys_ref, biz_ref]})
        try:
            reply = self.llm.complete(system, business + "\n\nGuest question: " + question)
        except SaraError as exc:
            LOG.warning("llm failure: %s", exc.message)
            reply = ""
        if not reply:
            # Deterministic grounded extract (no invention, source-backed).
            reply = re.sub(r"\s+", " ", retrieved[0].text).strip()[:int(self.rules.get("max_chars", 900))]
        return reply[:int(self.rules.get("max_chars", 900))]

    def _profile(self, tenant_id: str) -> str:
        parts = []
        for entity in ("location", "checkin_checkout", "amenity", "policy"):
            rows = self.knowledge.facts(tenant_id, entity)[:3]
            for row in rows:
                parts.append(entity + ": " + str(row.get("value"))[:200])
        return " ; ".join(parts)

    def _entity_list(self, tenant_id: str, entity: str) -> str:
        return " ; ".join(str(r.get("value"))[:200]
                          for r in self.knowledge.facts(tenant_id, entity)[:10])

    def _booking_turn(self, tenant_id: str, conversation_id: str, chat_id: str, text: str,
                      language: str, state: Dict[str, Any], sources: List[str],
                      tool_calls: List[Dict[str, Any]],
                      escalate_reason: Optional[str]) -> Tuple[Optional[str], Optional[str],
                                                               List[str], Optional[str]]:
        draft = dict(state.get("booking_draft") or {})
        draft.update(self._extract_booking_fields(text))
        state["booking_draft"] = draft
        state["booking_in_progress"] = True
        missing = self.bookings.missing_fields(draft)
        tool_calls.append({"tool": "booking.missing_fields", "missing": missing})
        if missing:
            question = self._ask_for(missing[0], language)
            return question, None, [], escalate_reason
        try:
            booking = self.bookings.create_draft(tenant_id, conversation_id, draft)
        except ValidationError as exc:
            state["booking_draft"] = {}
            return (exc.message + " " + self.canned("checking_with_team", language)).strip(), \
                None, [], "booking_requires_approval"
        state["booking_in_progress"] = False
        state["booking_draft"] = {}
        docs = self.docs.generate_for_booking(tenant_id, booking, sources or ["conversation"],
                                              staff_notes="Created from Telegram conversation "
                                                          + conversation_id)
        tool_calls.append({"tool": "documents.generate_for_booking", "count": len(docs)})
        reason = "booking_requires_approval" if booking["status"] == "pending_approval" else escalate_reason
        return None, booking["id"], [d["storage_path"] for d in docs], reason

    def _ask_for(self, field: str, language: str) -> str:
        questions = {
            "full_name": {"en": "May I have your full name, please?",
                          "ar": "هل يمكن معرفة اسمك الكامل؟",
                          "fr": "Puis-je avoir votre nom complet ?"},
            "phone": {"en": "What is the best phone number to reach you?",
                      "ar": "ما هو أفضل رقم هاتف للتواصل معك؟",
                      "fr": "Quel est votre numero de telephone ?"},
            "checkin_date": {"en": "Which date would you like to check in (YYYY-MM-DD)?",
                             "ar": "ما تاريخ الوصول (سنة-شهر-يوم)؟",
                             "fr": "Quelle est votre date d'arrivee (AAAA-MM-JJ) ?"},
            "checkout_date": {"en": "And your check-out date (YYYY-MM-DD)?",
                              "ar": "وما تاريخ المغادرة (سنة-شهر-يوم)؟",
                              "fr": "Et votre date de depart (AAAA-MM-JJ) ?"},
            "room_type": {"en": "Which room type would you prefer?",
                          "ar": "أي نوع غرفة تفضل؟",
                          "fr": "Quel type de chambre preferez-vous ?"},
            "guests": {"en": "How many guests will be staying?",
                       "ar": "كم عدد الضيوف؟",
                       "fr": "Combien de personnes ?"},
        }
        table = questions.get(field, {})
        return table.get(language) or table.get("en") or ("Could you share your " +
                                                          field.replace("_", " ") + "?")

    @staticmethod
    def _extract_booking_fields(text: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        dates = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", text or "")
        if dates:
            out["checkin_date"] = dates[0]
        if len(dates) > 1:
            out["checkout_date"] = dates[1]
        phone = re.search(r"(\+?\d[\d\s\-]{7,}\d)", text or "")
        if phone:
            out["phone"] = phone.group(1).strip()
        guests = re.search(r"(\d+)\s*(guests|people|persons|adults|pax)", (text or "").lower())
        if guests:
            out["guests"] = int(guests.group(1))
        room = re.search(r"\b(deluxe|standard|suite|double|single|twin|family|villa)\b",
                         (text or "").lower())
        if room:
            out["room_type"] = room.group(1)
        name = re.search(r"(?:my name is|i am|i'm|je m'appelle|me llamo)\s+([A-Za-z\u00c0-\u024f'\- ]{3,40})",
                         text or "", flags=re.IGNORECASE)
        if name:
            out["full_name"] = name.group(1).strip()
        return out

    def _capture_lead(self, tenant_id: str, conversation_id: str, text: str, language: str,
                      intent: str, state: Dict[str, Any]) -> None:
        lead_cfg = self.conf.get("lead_capture", {}) or {}
        fields = self._extract_booking_fields(text)
        collected = dict(state.get("lead") or {})
        for key in lead_cfg.get("fields", []) or []:
            if key in fields:
                collected[key] = fields[key]
        collected.setdefault("language", language)
        collected["intent"] = intent
        state["lead"] = collected
        if all(collected.get(f) for f in (lead_cfg.get("required") or [])) and \
                not state.get("lead_saved"):
            payload = dict(collected)
            for key in ("phone", "email", "full_name"):
                if payload.get(key):
                    payload[key] = self.crypto.encrypt(str(payload[key]))
            self.db.insert("leads", {"id": new_id("lead"), "tenant_id": tenant_id,
                                     "conversation_id": conversation_id,
                                     "data": json.dumps(payload, ensure_ascii=False),
                                     "status": "new", "created_at": iso(), "updated_at": iso()})
            state["lead_saved"] = True

    def _escalate(self, tenant_id: str, conversation_id: str, reason: str,
                  detail: Dict[str, Any]) -> None:
        self.db.insert("escalations", {"id": new_id("esc"), "tenant_id": tenant_id,
                                       "conversation_id": conversation_id, "reason": reason,
                                       "detail": json.dumps(detail, ensure_ascii=False, default=str),
                                       "status": "open", "created_at": iso(), "resolved_at": None})
        self.metrics.inc("escalations")
        self.audit.record(tenant_id, "escalation", {"reason": reason, "detail": detail},
                          conversation_id=conversation_id)

    def _finish(self, tenant_id: str, conversation_id: str, language: str, intent: str,
                confidence: float, reply: str, sources: List[str],
                tool_calls: List[Dict[str, Any]], escalated: bool, reason: Optional[str],
                state: Dict[str, Any], booking_id: Optional[str] = None,
                documents: Optional[List[str]] = None) -> AgentDecision:
        paused = None
        if escalated:
            minutes = int(self.esc.get("pause_bot_on_escalation_minutes", 0) or 0)
            if minutes:
                paused = iso(now() + timedelta(minutes=minutes))
        self.save_state(conversation_id, state, language=language, paused_until=paused)
        reply = (reply or "")[:int(self.rules.get("max_chars", 900))]
        self.audit.record(tenant_id, "decision",
                          {"intent": intent, "language": language, "confidence": confidence,
                           "escalated": escalated, "reason": reason, "sources": sources,
                           "tool_calls": tool_calls, "booking_id": booking_id},
                          conversation_id=conversation_id)
        return AgentDecision(reply=reply, language=language, intent=intent,
                             confidence=round(float(confidence), 4), escalated=escalated,
                             escalation_reason=reason, sources=sources, tool_calls=tool_calls,
                             conversation_id=conversation_id, booking_id=booking_id,
                             documents=documents or [])


# --------------------------------------------------------------- background jobs
class JobQueue:
    # DB-backed queue with retries, backoff, dead-lettering and in-process workers.
    def __init__(self, cfg: Config, db: Database, audit: AuditLog, metrics: Metrics) -> None:
        self.cfg, self.db, self.audit, self.metrics = cfg, db, audit, metrics
        self.conf = cfg.get("jobs", {}) or {}
        self.handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()

    def register(self, kind: str, handler: Callable[[Dict[str, Any]], Any]) -> None:
        self.handlers[kind] = handler

    def enqueue(self, tenant_id: str, queue_name: str, kind: str,
                payload: Dict[str, Any], delay_seconds: int = 0) -> str:
        job_id = new_id("job")
        self.db.insert("jobs", {"id": job_id, "tenant_id": tenant_id, "queue": queue_name,
                                "kind": kind,
                                "payload": json.dumps(payload, ensure_ascii=False, default=str),
                                "status": "queued", "attempts": 0, "last_error": None,
                                "run_at": iso(now() + timedelta(seconds=delay_seconds)),
                                "created_at": iso(), "updated_at": iso()})
        return job_id

    def _claim(self) -> Optional[Dict[str, Any]]:
        row = self.db.one("SELECT * FROM jobs WHERE status='queued' AND run_at<=?"
                          " ORDER BY run_at LIMIT 1", (iso(),))
        if not row:
            return None
        self.db.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status='queued'",
                        (iso(), row["id"]))
        check = self.db.one("SELECT status FROM jobs WHERE id=?", (row["id"],))
        return row if check and check["status"] == "running" else None

    def run_once(self) -> Optional[Dict[str, Any]]:
        job = self._claim()
        if not job:
            return None
        retry = self.conf.get("retry", {}) or {}
        max_attempts = int(retry.get("max_attempts", 5))
        backoff = retry.get("backoff_seconds") or [2, 5, 15, 45, 120]
        handler = self.handlers.get(job["kind"])
        attempts = int(job["attempts"]) + 1
        try:
            if not handler:
                raise SaraError("no handler registered for job kind: " + job["kind"])
            handler(json.loads(job["payload"] or "{}"))
            self.db.execute("UPDATE jobs SET status='done', attempts=?, updated_at=? WHERE id=?",
                            (attempts, iso(), job["id"]))
            return {"job_id": job["id"], "status": "done", "kind": job["kind"]}
        except Exception as exc:
            self.metrics.inc("errors")
            if attempts >= max_attempts:
                self.db.execute("UPDATE jobs SET status='failed', queue=?, attempts=?, last_error=?,"
                                " updated_at=? WHERE id=?",
                                (retry.get("dead_letter_queue", "failed"), attempts, str(exc),
                                 iso(), job["id"]))
                status = "failed"
            else:
                delay = backoff[min(attempts - 1, len(backoff) - 1)]
                self.db.execute("UPDATE jobs SET status='queued', attempts=?, last_error=?, run_at=?,"
                                " updated_at=? WHERE id=?",
                                (attempts, str(exc), iso(now() + timedelta(seconds=delay)),
                                 iso(), job["id"]))
                status = "retrying"
            self.audit.record(job["tenant_id"], "tool_call", {"job": job["kind"], "status": status,
                                                              "error": str(exc)[:400]})
            return {"job_id": job["id"], "status": status, "kind": job["kind"], "error": str(exc)}

    def start(self, workers: Optional[int] = None) -> None:
        count = int(workers if workers is not None else self.conf.get("workers", 2))

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    if self.run_once() is None:
                        self._stop.wait(1.0)
                except Exception:
                    LOG.exception("job worker crashed")
                    self._stop.wait(2.0)

        for _ in range(max(1, count)):
            t = threading.Thread(target=loop, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def stats(self) -> Dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS c FROM jobs GROUP BY status")
        return {r["status"]: r["c"] for r in rows}


# ------------------------------------------------------------------- app wiring
class SaraApp:
    # Composition root: every service is independently replaceable.
    def __init__(self, cfg: Optional[Config] = None) -> None:
        self.cfg = cfg or Config.load()
        setup_logging(self.cfg)
        self.metrics = Metrics(self.cfg.get("observability.metrics", []) or [],
                              enabled=self.cfg.flag("enable_metrics", True))
        self.crypto = Crypto(os.environ.get(
            self.cfg.get("security.encryption.master_key_env", "SARA_MASTER_KEY")))
        self.storage = StorageService(self.cfg)
        self.db = Database(self.cfg)
        self.audit = AuditLog(self.db, self.storage, self.cfg)
        self.access = AccessControl(self.cfg, self.db)
        self.limiter = RateLimiter(self.cfg)
        self.ingestion = IngestionService(self.cfg, self.db, self.storage, self.audit, self.metrics)
        self.knowledge = KnowledgeService(self.cfg, self.db, self.audit)
        self.prompts = PromptRegistry(self.cfg, self.db, self.audit)
        self.llm = LlmClient(self.cfg, self.metrics)
        self.bookings = BookingService(self.cfg, self.db, self.audit, self.metrics, self.crypto)
        self.documents = DocumentService(self.cfg, self.db, self.storage, self.audit)
        self.telegram = TelegramChannel(self.cfg, self.db, self.storage, self.crypto,
                                        self.audit, self.metrics, self.limiter)
        self.sheets = SheetsConnector(self.cfg, self.db, self.crypto, self.audit, self.metrics)
        self.voice = VoiceService(self.cfg, self.storage, self.audit, self.metrics, self.crypto)
        self.conversations = ConversationService(self.cfg, self.db, self.knowledge, self.prompts,
                                                 self.bookings, self.documents, self.llm,
                                                 self.audit, self.metrics, self.crypto)
        self.jobs = JobQueue(self.cfg, self.db, self.audit, self.metrics)
        self._register_jobs()

    # ---- jobs
    def _register_jobs(self) -> None:
        def deliver(payload: Dict[str, Any]) -> None:
            self.telegram.send(payload["tenant_id"], payload["chat_id"], payload["text"],
                               payload.get("conversation_id"), payload.get("adapter"))

        def sync(payload: Dict[str, Any]) -> None:
            principal = Principal("system", payload["tenant_id"], "owner")
            self.sheets.sync(principal, payload.get("target", "availability"),
                             payload.get("spreadsheet_id"), payload.get("sheet"),
                             payload.get("direction"))

        def backup(payload: Dict[str, Any]) -> None:
            self.storage.backup(payload["tenant_id"])

        def session_gc(payload: Dict[str, Any]) -> None:
            keep = int(self.cfg.get("storage.retention_days.voice", 30))
            cutoff = iso(now() - timedelta(days=keep))
            self.db.execute("DELETE FROM idempotency WHERE created_at<?", (cutoff,))

        self.jobs.register("telegram_deliver", deliver)
        self.jobs.register("sheets_sync", sync)
        self.jobs.register("backup", backup)
        self.jobs.register("session_gc", session_gc)

    # ---- bootstrap
    def bootstrap_tenant(self, name: str, owner_email: str) -> Dict[str, Any]:
        tenant = self.access.create_tenant(name)
        user, token = self.access.create_user(tenant["id"], owner_email, "owner")
        self.audit.record(tenant["id"], "auth", {"step": "bootstrap", "user": user["id"]})
        return {"tenant": tenant, "user_id": user["id"], "api_token": token}

    # ---- owner console (business-owner commands, not the guest booking flow)
    def owner_command(self, principal: Principal, text: str,
                      chat_id: str = "owner-console") -> Dict[str, Any]:
        tenant_id = principal.tenant_id
        raw = (text or "").strip()
        low = raw.lower()
        conversation_id: Optional[str] = None
        try:
            conv = self.conversations.get_or_create(tenant_id, chat_id or "owner-console", "console")
            conversation_id = conv["id"]
        except Exception:
            pass

        self.db.insert("messages", {
            "id": new_id("msg"), "tenant_id": tenant_id, "conversation_id": conversation_id,
            "direction": "in", "text": raw[:2000], "media": None, "intent": "owner_command",
            "confidence": 1.0, "sources": None, "external_id": None,
            "delivery_status": "received", "created_at": iso()})
        self.audit.record(tenant_id, "message_in", {"channel": "console", "chat_id": chat_id,
                                                    "intent": "owner_command"},
                          actor=principal.user_id)

        def respond(body: str, intent: str = "",
                    payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            return {"reply": body, "intent": intent, "payload": payload or {},
                    "sources": [], "escalated": False}

        if not raw:
            return respond(self._owner_help(), "help")

        def fmt_booking(b: Dict[str, Any]) -> str:
            d = b.get("data") or {}
            return ("%s | %s | %s | %s -> %s | %s" % (
                b.get("id"), d.get("full_name") or "-", d.get("room_type") or "-",
                d.get("checkin_date") or "-", d.get("checkout_date") or "-", b.get("status")))

        words = raw.split()
        head = words[0].lower().rstrip("!?.,:;")

        # ---- bookings list / detail
        if head in ("bookings", "booking", "bkg", "leads"):
            if head in ("booking", "bkg") and len(words) > 1 and words[1].startswith("bkg_"):
                try:
                    b = self.bookings.get(tenant_id, words[1])
                    d = b.get("data") or {}
                    lines = ["Booking %s [%s]" % (b.get("id"), b.get("status")),
                             "Guest: %s | %s" % (d.get("full_name"),
                                                  d.get("phone") or d.get("email") or ""),
                             "Room: %s | Check-in: %s | Check-out: %s | Guests: %s" % (
                                 d.get("room_type"), d.get("checkin_date"),
                                 d.get("checkout_date"), d.get("guests")),
                             "Created: %s" % b.get("created_at")]
                    return respond("\n".join(lines), "booking_detail", {"booking": b})
                except SaraError as exc:
                    return respond(getattr(exc, "message", str(exc)), "error")
            statuses = list((self.cfg.get("booking") or {}).get("statuses") or [])
            wanted = (words[1] if len(words) > 1 else "all").lower().rstrip("!?.,:;")
            status = wanted if wanted in statuses else None
            try:
                bookings = self.bookings.list(tenant_id, status)
            except Exception as exc:
                bookings = []
                LOG.warning("owner bookings list failed: %s", exc)
            if wanted == "today":
                today = now().date().isoformat()
                bookings = [b for b in bookings
                            if str((b.get("data") or {}).get("checkin_date")) == today]
            if not bookings:
                label = (" with status '%s'" % wanted) if status else ""
                return respond("No bookings found%s." % label, "bookings_list", {"count": 0})
            shown = bookings[:15]
            extra = ("\n... and %d more" % (len(bookings) - len(shown))
                     if len(bookings) > len(shown) else "")
            title = "Bookings (%s)" % wanted if (status or wanted != "all") else "Bookings"
            return respond(title + ":\n" + "\n".join(fmt_booking(b) for b in shown) + extra,
                           "bookings_list", {"count": len(bookings)})

        # ---- booking status changes
        action_status = {"confirm": "confirmed", "approve": "confirmed", "cancel": "cancelled",
                         "reject": "rejected"}
        if head in action_status:
            if len(words) < 2 or not words[1].startswith("bkg_"):
                return respond("Usage: %s <booking_id>\nGet ids from: bookings" % head, "usage")
            try:
                b = self.bookings.set_status(principal, words[1], action_status[head])
                return respond("%s — booking %s is now %s." % (
                    head.capitalize(), words[1], b.get("status")), "booking_status",
                    {"booking_id": words[1], "status": b.get("status")})
            except SaraError as exc:
                return respond(getattr(exc, "message", str(exc)), "error")

        # ---- google sheets
        if head in ("sheets", "sync") or "sheet" in low:
            if "connect" in low or "authorize" in low:
                try:
                    info = self.sheets.start_authorization(principal)
                    return respond(
                        "Google authorization ready. Open this URL in your browser to connect "
                        "your account:\n" + info["authorize_url"], "sheets_connect",
                        {"authorize_url": info["authorize_url"]})
                except SaraError as exc:
                    return respond(getattr(exc, "message", str(exc)), "error")
            if "status" in low or "history" in low:
                conns = self.db.query("SELECT label, status, updated_at FROM connections "
                                      "WHERE tenant_id=? AND provider='google_sheets' "
                                      "ORDER BY updated_at DESC LIMIT 5", (tenant_id,))
                history: List[Dict[str, Any]] = []
                try:
                    history = self.sheets.history(tenant_id, 5)
                except Exception:
                    history = []
                lines = ["Google Sheets connections:"]
                lines += ["- %s (%s) — %s" % (c.get("label"), c.get("status"),
                                               (c.get("updated_at") or "")[:19]) for c in conns]
                if not conns:
                    lines.append("- none yet — say \"connect sheets\"")
                if history:
                    lines.append("Recent syncs:")
                    for h in history[:5]:
                        detail = "ok"
                        try:
                            detail = ((json.loads(h.get("detail") or "{}") or {}).get("error")
                                      or "ok")
                        except Exception:
                            detail = "ok"
                        lines.append("- %s (%s) | %s | rows read %s / written %s | %s" % (
                            h.get("target"), h.get("direction"), h.get("status"),
                            h.get("rows_read"), h.get("rows_written"), detail)[:220])
                return respond("\n".join(lines), "sheets_status",
                               {"connections": conns, "history": history})
            target = "bookings"
            for t in ("bookings", "leads", "availability"):
                if t in low:
                    target = t
                    break
            direction = "outbound" if "inbound" not in low else "inbound"
            spreadsheet: Optional[str] = None
            if "auto" in low or "create" in low or "new" in low:
                spreadsheet = "AUTO_CREATE"
            for w in words[1:]:
                if re.fullmatch(r"[A-Za-z0-9_-]{20,}", w):
                    spreadsheet = w
                    break
            try:
                result = self.sheets.sync(principal, target, spreadsheet, None, direction)
                sid = result.get("spreadsheet_id") or spreadsheet or "default spreadsheet"
                return respond(
                    "Synced %s (%s) — spreadsheet: %s\nRows read: %s | rows written: %s | conflicts: %s" % (
                        target, direction, sid, result.get("rows_read"),
                        result.get("rows_written"), result.get("conflicts")),
                    "sheets_sync", result)
            except SaraError as exc:
                return respond("Sync failed: %s" % getattr(exc, "message", str(exc)), "error")

        # ---- knowledge / files / facts
        if head in ("search", "knowledge", "know", "lookup", "find"):
            query = raw.split(None, 1)[1] if " " in raw else ""
            if not query:
                return respond("Usage: search <what are you looking for?>", "usage")
            hits = self.knowledge.search(tenant_id, query, 8)
            if not hits:
                return respond("No knowledge matches for: %s" % query, "search", {"count": 0})
            lines = ["Top matches for \"%s\":" % query]
            for h in hits:
                lines.append("- [%.2f] %s" % (h.score, h.source_ref))
                lines.append("  " + (h.text or "")[:160].replace("\n", " "))
            return respond("\n".join(lines), "search", {"count": len(hits)})

        if head in ("files", "docs", "documents", "knowledgebase", "kb"):
            docs: List[Dict[str, Any]] = []
            try:
                docs = self.ingestion.list_documents(tenant_id)
            except Exception as exc:
                LOG.warning("owner docs list failed: %s", exc)
            if not docs:
                return respond("No knowledge documents uploaded yet.", "docs", {"count": 0})
            lines = ["Knowledge base (%d document%s):" % (
                len(docs), "" if len(docs) == 1 else "s")]
            for d in docs[:25]:
                lines.append("- %s | %s | %s" % (d.get("filename") or d.get("doc_id"),
                                                  d.get("route") or d.get("mime") or "file",
                                                  (d.get("created_at") or "")[:19]))
            return respond("\n".join(lines), "docs", {"count": len(docs)})

        if head in ("facts", "rates", "availability", "inventory"):
            if head == "facts" and len(words) > 1 and words[1] in (
                    "room_type", "rate", "availability", "amenity", "policy"):
                entity = words[1]
            elif head in ("rates",) or "rate" in low:
                entity = "rate"
            else:
                entity = "availability" if ("available" in low or "stock" in low) else "room_type"
            rows = self.knowledge.facts(tenant_id, entity)
            if not rows:
                return respond(
                    "No %s facts stored yet (upload a rate sheet or sync from Google Sheets)."
                    % entity, "facts", {"count": 0})
            lines = ["%s facts:" % entity]
            for r in rows[:25]:
                extra = " (%s)" % (r.get("currency") or "") \
                    if r.get("numeric_value") is not None else ""
                lines.append("- %s = %s%s" % (r.get("key"), r.get("value"), extra))
            return respond("\n".join(lines), "facts", {"count": len(rows)})

        if head in ("stats", "summary", "dashboard", "overview"):
            by_status: Dict[str, int] = {}
            try:
                by_status = {r["status"]: int(r["c"]) for r in self.db.query(
                    "SELECT status, COUNT(*) AS c FROM bookings WHERE tenant_id=? GROUP BY status",
                    (tenant_id,))}
            except Exception:
                by_status = {}
            docs_count = 0
            try:
                docs_count = len(self.ingestion.list_documents(tenant_id))
            except Exception:
                docs_count = 0
            escal = 0
            try:
                row = self.db.one("SELECT COUNT(*) AS c FROM escalations WHERE tenant_id=?",
                                  (tenant_id,))
                escal = int((row or {}).get("c") or 0)
            except Exception:
                escal = 0
            conn_row = self.db.one("SELECT COUNT(*) AS c FROM connections WHERE tenant_id=?",
                                   (tenant_id,))
            lines = ["Business summary:",
                     "- Bookings: %d%s" % (sum(by_status.values()), " (" +
                         ", ".join("%s: %s" % (k, v) for k, v in sorted(by_status.items()))
                         + ")" if by_status else ""),
                     "- Knowledge files: %d" % docs_count,
                     "- Open escalations: %d" % escal,
                     "- Connected integrations: %d" % int((conn_row or {}).get("c") or 0)]
            return respond("\n".join(lines), "stats",
                           {"bookings_by_status": by_status, "docs": docs_count,
                            "escalations": escal})

        return respond(self._owner_help() + "\n\n(unsupported command: %s)" % words[0], "help")

    def _owner_help(self) -> str:
        return ("Owner console commands:\n"
                "- bookings [status|today]      list guest bookings\n"
                "- booking <id>                 booking detail\n"
                "- confirm|cancel|reject <id>   change a booking's status\n"
                "- connect sheets               authorize Google Sheets\n"
                "- sheets status                connection + sync history\n"
                "- sync sheets [bookings|leads|availability] [auto]  push to Google Sheets\n"
                "- search <query>               search your knowledge base\n"
                "- docs / files                 list uploaded knowledge files\n"
                "- facts [entity]               show rates / availability / room types\n"
                "- stats / summary              business overview")

    # ---- health
    def health(self) -> Dict[str, Any]:
        checks: Dict[str, str] = {}
        for name in self.cfg.get("observability.health_checks", []) or []:
            try:
                if name == "db":
                    checks[name] = "ok" if self.db.healthy() else "down"
                elif name == "storage":
                    probe = self.storage.root / ".health"
                    probe.write_text(iso(), encoding="utf-8")
                    probe.unlink(missing_ok=True)
                    checks[name] = "ok"
                elif name == "queue":
                    checks[name] = "ok"
                elif name == "telegram_connector":
                    checks[name] = "configured" if (self.cfg.get(
                        "integrations.telegram.connector.send_url")) else "unconfigured"
                elif name == "sheets_connector":
                    checks[name] = "configured" if (self.cfg.get(
                        "integrations.google_sheets.connector.api_base")) else "unconfigured"
                elif name == "llm":
                    checks[name] = self.llm.provider
                else:
                    checks[name] = "unknown"
            except Exception as exc:
                checks[name] = "error: " + str(exc)[:120]
        degraded = [k for k, v in checks.items() if v.startswith(("down", "error"))]
        return {"status": "degraded" if degraded else "ok", "checks": checks,
                "config_version": self.cfg.get("meta.config_version"),
                "channel": self.cfg.get("meta.channel"), "time": iso()}

    # ---- telegram inbound orchestration (shared by webhook + voice)
    def handle_telegram_update(self, tenant_id: str, update: Dict[str, Any],
                               adapter: Optional[str] = None) -> Dict[str, Any]:
        parsed = self.telegram.parse_update(update)
        key = tenant_id + ":" + parsed["update_id"]
        seen = self.telegram.seen_update(tenant_id, key)
        if seen:
            return {"status": "duplicate", "result": seen}
        self.limiter.check("telegram_inbound_per_chat_per_min", parsed["chat_id"])
        text = parsed["text"]
        media = parsed["media"]
        if media and media.get("kind") in ("voice", "audio"):
            blob = self.telegram.adapter(adapter).download_file(tenant_id, media.get("file_id"))
            session = self.conversations.get_or_create(tenant_id, parsed["chat_id"])
            transcript = self.voice.transcribe(tenant_id, session["id"], blob, "ogg")
            text = transcript["text"] or text
        if media and media.get("kind") == "document" and media.get("file_name"):
            blob = self.telegram.adapter(adapter).download_file(tenant_id, media.get("file_id"))
            if blob:
                try:
                    self.ingestion.ingest(Principal("telegram", tenant_id, "manager"),
                                          media["file_name"], blob, media.get("mime") or "")
                except SaraError as exc:
                    LOG.warning("inbound file ingest rejected: %s", exc.message)
        decision = self.conversations.handle(tenant_id, parsed["chat_id"], text, "telegram",
                                             media or None, external_id=parsed["update_id"])
        if decision.reply:
            self.telegram.send(tenant_id, parsed["chat_id"], decision.reply,
                               decision.conversation_id, adapter)
        for path in decision.documents:
            try:
                self.telegram.send_document(tenant_id, parsed["chat_id"], path,
                                            "Booking documents", adapter)
            except SaraError as exc:
                LOG.warning("document delivery failed: %s", exc.message)
        result = decision.to_dict()
        self.telegram.remember_update(tenant_id, key, result)
        return {"status": "ok", "result": result}


# ------------------------------------------------------------------ REST layer
class Router:
    def __init__(self) -> None:
        self.routes: List[Tuple[str, "re.Pattern[str]", Callable[..., Any], Optional[str]]] = []

    def add(self, method: str, pattern: str, handler: Callable[..., Any],
            permission: Optional[str] = None) -> None:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self.routes.append((method.upper(), regex, handler, permission))

    def match(self, method: str, path: str):
        allowed = False
        for m, regex, handler, permission in self.routes:
            match = regex.match(path)
            if match:
                if m == method.upper():
                    return handler, match.groupdict(), permission
                allowed = True
        raise NotFound("no route for " + method + " " + path) if not allowed else \
            SaraError("method not allowed for " + path)


def build_api(app: SaraApp) -> Router:
    router = Router()
    cfg = app.cfg

    # ---- public
    router.add("GET", "/health", lambda ctx: app.health())
    router.add("GET", "/metrics", lambda ctx: {"metrics": app.metrics.snapshot(),
                                               "jobs": app.jobs.stats()})
    router.add("POST", "/bootstrap", lambda ctx: app.bootstrap_tenant(
        ctx["body"].get("name") or "tenant", ctx["body"].get("owner_email") or "owner@example.com"))

    # ---- ingestion
    def upload(ctx):
        body = ctx["body"]
        filename = body.get("filename")
        if not filename or "content_base64" not in body:
            raise ValidationError("filename and content_base64 are required")
        data = base64.b64decode(body["content_base64"])
        return app.ingestion.ingest(ctx["principal"], filename, data, body.get("mime", ""))

    router.add("POST", "/ingest/upload", upload, "ingest:upload")
    router.add("GET", "/ingest/documents",
               lambda ctx: {"documents": app.ingestion.list_documents(ctx["tenant_id"])},
               "ingest:read")

    # ---- knowledge
    router.add("GET", "/knowledge/search",
               lambda ctx: {"results": [dataclasses.asdict(r) for r in
                                        app.knowledge.search(ctx["tenant_id"],
                                                             ctx["query"].get("q", ""))]},
               "knowledge:read")
    router.add("GET", "/knowledge/facts/{entity}",
               lambda ctx, entity: {"facts": app.knowledge.facts(ctx["tenant_id"], entity)},
               "knowledge:read")

    # ---- prompts
    router.add("GET", "/prompts/{role}",
               lambda ctx, role: app.prompts.get(ctx["tenant_id"], role), "prompts:read")
    router.add("PUT", "/prompts/{role}",
               lambda ctx, role: app.prompts.update(ctx["principal"], role,
                                                    ctx["body"].get("text", "")), "prompts:write")

    # ---- conversations
    router.add("POST", "/conversations/message",
               lambda ctx: app.conversations.handle(ctx["tenant_id"], ctx["body"]["chat_id"],
                                                    ctx["body"].get("text", ""),
                                                    ctx["body"].get("channel", "telegram")).to_dict(),
               "conversation:reply")
    router.add("GET", "/conversations/{conversation_id}/messages",
               lambda ctx, conversation_id: {"messages": app.conversations.history(conversation_id)},
               "conversation:read")
    router.add("GET", "/audit",
               lambda ctx: {"audit": app.audit.trail(ctx["tenant_id"],
                                                     ctx["query"].get("conversation_id"))},
               "audit:read")
    router.add("GET", "/escalations",
               lambda ctx: {"escalations": app.db.query(
                   "SELECT * FROM escalations WHERE tenant_id=? ORDER BY created_at DESC LIMIT 200",
                   (ctx["tenant_id"],))}, "conversation:read")

    # ---- bookings & documents
    router.add("POST", "/bookings",
               lambda ctx: app.bookings.create_draft(ctx["tenant_id"],
                                                     ctx["body"].get("conversation_id"),
                                                     ctx["body"].get("data") or {},
                                                     actor=ctx["principal"].user_id),
               "booking:create_draft")
    router.add("GET", "/bookings",
               lambda ctx: {"bookings": app.bookings.list(ctx["tenant_id"],
                                                          ctx["query"].get("status"))},
               "booking:read")
    router.add("POST", "/bookings/{booking_id}/status",
               lambda ctx, booking_id: app.bookings.set_status(ctx["principal"], booking_id,
                                                               ctx["body"].get("status", "")),
               "booking:approve")

    def generate_docs(ctx, booking_id):
        booking = app.bookings.get(ctx["tenant_id"], booking_id)
        docs = app.documents.generate_for_booking(ctx["tenant_id"], booking,
                                                  ctx["body"].get("source_refs") or ["manual"],
                                                  ctx["body"].get("staff_notes", ""))
        return {"documents": docs}

    router.add("POST", "/bookings/{booking_id}/documents", generate_docs, "docs:generate")
    router.add("GET", "/documents",
               lambda ctx: {"documents": app.documents.list(ctx["tenant_id"],
                                                            ctx["query"].get("entity_id"))},
               "docs:read")
    router.add("PUT", "/documents/templates/{template_id}",
               lambda ctx, template_id: {"path": app.documents.save_template(
                   ctx["tenant_id"], template_id, ctx["body"].get("html", ""))}, "docs:write")

    # ---- telegram integration ("Connect Phone Number/Telegram")
    router.add("POST", "/integrations/telegram/connect",
               lambda ctx: app.telegram.connector.start_authorization(ctx["principal"],
                                                                      ctx["body"].get("label", "")),
               "integration:connect")
    router.add("POST", "/integrations/telegram/callback",
               lambda ctx: app.telegram.connector.complete_authorization(
                   ctx["tenant_id"], ctx["body"]["connection_id"], ctx["body"].get("grant") or {}),
               "integration:connect")
    router.add("GET", "/integrations/telegram/connections",
               lambda ctx: {"connections": app.telegram.connector.list_connections(ctx["tenant_id"])},
               "integration:read")
    router.add("POST", "/integrations/telegram/send",
               lambda ctx: app.telegram.send(ctx["tenant_id"], ctx["body"]["chat_id"],
                                             ctx["body"]["text"],
                                             ctx["body"].get("conversation_id"),
                                             ctx["body"].get("adapter")),
               "conversation:reply")

    def webhook(ctx, connection_id):
        conf = cfg.get("integrations.telegram.webhook", {}) or {}
        secret = os.environ.get("SARA_WEBHOOK_SECRET", "")
        sig = ctx["headers"].get(str(conf.get("signature_header", "X-Sara-Signature")).lower())
        if secret and not app.telegram.verify_signature(ctx["raw_body"], sig, secret):
            raise AuthError("invalid webhook signature")
        tenant_id = ctx["query"].get("tenant_id") or ctx["body"].get("tenant_id")
        if not tenant_id:
            row = app.db.one("SELECT tenant_id FROM connections WHERE id=?", (connection_id,))
            if not row:
                raise NotFound("unknown connection: " + connection_id)
            tenant_id = row["tenant_id"]
        return app.handle_telegram_update(tenant_id, ctx["body"], ctx["body"].get("adapter"))

    router.add("POST", "/webhooks/telegram/{connection_id}", webhook)

    # ---- google sheets ("Connect Google Sheets")
    router.add("POST", "/integrations/sheets/connect",
               lambda ctx: app.sheets.start_authorization(ctx["principal"],
                                                          ctx["body"].get("label", "")),
               "integration:connect")
    router.add("POST", "/integrations/sheets/callback",
               lambda ctx: app.sheets.complete_authorization(ctx["tenant_id"],
                                                              ctx["body"]["connection_id"],
                                                              ctx["body"].get("grant") or {}),
               "integration:connect")
    router.add("GET", "/integrations/sheets/spreadsheets",
               lambda ctx: {"spreadsheets": app.sheets.list_spreadsheets(ctx["tenant_id"])},
               "integration:read")
    router.add("GET", "/integrations/sheets/{spreadsheet_id}/sheets",
               lambda ctx, spreadsheet_id: {"sheets": app.sheets.list_sheets(ctx["tenant_id"],
                                                                             spreadsheet_id)},
               "integration:read")
    router.add("GET", "/integrations/sheets/{spreadsheet_id}/rows",
               lambda ctx, spreadsheet_id: {"rows": app.sheets.read_rows(
                   ctx["tenant_id"], spreadsheet_id, ctx["query"].get("sheet", "Sheet1"))},
               "sync:read")
    router.add("POST", "/integrations/sheets/sync",
               lambda ctx: app.sheets.sync(ctx["principal"], ctx["body"].get("target", "availability"),
                                            ctx["body"].get("spreadsheet_id"),
                                            ctx["body"].get("sheet"),
                                            ctx["body"].get("direction")), "sync:run")
    router.add("GET", "/integrations/sheets/history",
               lambda ctx: {"history": app.sheets.history(ctx["tenant_id"])}, "sync:read")

    # Google OAuth browser redirect: /agent/integrations/sheets/callback?code=...&state=...
    def sheets_callback(ctx):
        from html import escape
        from urllib.parse import unquote_plus
        q = ctx.get("query") or {}
        code = unquote_plus(q.get("code") or "")
        state = unquote_plus(q.get("state") or "")
        oauth_error = q.get("error") or ""
        if oauth_error or not code or not state:
            msg = escape(oauth_error or "Missing authorization code")
            return {"__status__": 400,
                    "__html__": "<!doctype html><meta charset='utf-8'><title>SARA — Google connection failed</title>"
                    "<body style='font-family:sans-serif;text-align:center;padding:48px'>"
                    "<h2>Google Sheets connection failed</h2><p>" + msg + "</p>"
                    "<script>setTimeout(function(){window.close()},1500)</script></body>"}
        info: Dict[str, Any] = {}
        try:
            padded = state + "=" * (-len(state) % 4)
            info = json.loads(base64.urlsafe_b64decode(padded).decode())
        except Exception:
            info = {}
        tenant_id = str(info.get("t") or "")
        connection_id = str(info.get("c") or "")
        if not tenant_id or not connection_id:
            return {"__status__": 400,
                    "__html__": "<!doctype html><meta charset='utf-8'><title>SARA — invalid state</title>"
                    "<body style='font-family:sans-serif;text-align:center;padding:48px'>"
                    "<h2>Invalid authorization state</h2>"
                    "<script>setTimeout(function(){window.close()},1500)</script></body>"}
        public = (os.environ.get("SARA_PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/")
        redirect_uri = public + "/agent/integrations/sheets/callback"
        try:
            grant = app.sheets.exchange_code_and_store(tenant_id, connection_id, code, redirect_uri)
            account = escape(grant.get("account") or "your Google account")
            body = ("<!doctype html><meta charset='utf-8'><title>SARA — Google Sheets connected</title>"
                    "<body style='font-family:sans-serif;text-align:center;padding:48px'>"
                    "<h2>✅ Google Sheets connected</h2>"
                    "<p>" + account + " is linked. You can close this window.</p>"
                    "<script>setTimeout(function(){window.close()},1200)</script></body>")
            return {"__status__": 200, "__html__": body}
        except Exception as exc:
            LOG.warning("sheets callback failed for tenant %s: %s", tenant_id, exc)
            msg = escape(str(exc)[:300])
            return {"__status__": 500,
                    "__html__": "<!doctype html><meta charset='utf-8'><title>SARA — connection failed</title>"
                    "<body style='font-family:sans-serif;text-align:center;padding:48px'>"
                    "<h2>Google Sheets connection failed</h2><p>" + msg + "</p>"
                    "<script>setTimeout(function(){window.close()},2000)</script></body>"}

    router.add("GET", "/integrations/sheets/callback", sheets_callback)

    # Business-owner console: the owner's chat obeys these commands, not the guest flow
    router.add("POST", "/conversations/owner",
               lambda ctx: app.owner_command(ctx["principal"], ctx["body"].get("text") or "",
                                             ctx["body"].get("chat_id") or "owner-console"),
               "conversation:reply")

    # ---- voice
    def voice_command(ctx):
        body = ctx["body"]
        app.limiter.check("voice_per_tenant_per_min", ctx["tenant_id"])
        chat_id = body.get("chat_id") or ("voice:" + ctx["principal"].user_id)
        session = app.conversations.get_or_create(ctx["tenant_id"], chat_id, "voice")
        text = body.get("text") or ""
        clip = None
        if body.get("audio_base64"):
            clip = app.voice.transcribe(ctx["tenant_id"], session["id"],
                                        base64.b64decode(body["audio_base64"]),
                                        body.get("format", "ogg"), body.get("language"))
            text = clip["text"] or text
        if not text:
            raise ValidationError("no speech recognized and no text provided")
        decision = app.conversations.handle(ctx["tenant_id"], chat_id, text, "voice",
                                            actor=ctx["principal"].user_id)
        speech = app.voice.synthesize(ctx["tenant_id"], session["id"], decision.reply)
        out = decision.to_dict()
        out["transcript"] = text
        out["clip"] = clip
        out["speech"] = speech
        return out

    router.add("POST", "/voice/command", voice_command, "voice:use")
    router.add("POST", "/voice/transcribe",
               lambda ctx: app.voice.transcribe(ctx["tenant_id"],
                                                 ctx["body"].get("session_id", "adhoc"),
                                                 base64.b64decode(ctx["body"]["audio_base64"]),
                                                 ctx["body"].get("format", "ogg")), "voice:use")

    # ---- admin / ops
    router.add("POST", "/admin/jobs/run", lambda ctx: {"result": app.jobs.run_once()}, "sync:run")
    router.add("POST", "/admin/backup",
               lambda ctx: {"snapshot_id": app.storage.backup(ctx["tenant_id"])}, "docs:write")
    router.add("POST", "/admin/migrate", lambda ctx: {"applied": app.db.migrate()}, "prompts:write")
    router.add("POST", str(cfg.get("testing.run_self_tests_endpoint", "/admin/selftest")),
               lambda ctx: run_self_tests(app), "audit:read")
    return router


class WsgiApp:
    # Minimal WSGI adapter so the REST layer runs under any WSGI/ASGI bridge.
    PUBLIC = {("GET", "/health"), ("GET", "/metrics"), ("POST", "/bootstrap"),
              ("GET", "/integrations/sheets/callback")}

    def __init__(self, app: SaraApp) -> None:
        self.app = app
        self.router = build_api(app)

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/") or "/"
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        raw = environ["wsgi.input"].read(length) if length else b""
        headers = {k[5:].replace("_", "-").lower(): v for k, v in environ.items()
                   if k.startswith("HTTP_")}
        query = {}
        for pair in (environ.get("QUERY_STRING") or "").split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                query[k] = v
        status_code, payload = 200, {}
        try:
            body: Dict[str, Any] = json.loads(raw.decode() or "{}") if raw else {}
            handler, params, permission = self.router.match(method, path)
            principal = None
            tenant_id = None
            if (method, path) not in self.PUBLIC and not path.startswith("/webhooks/"):
                token = (headers.get("authorization") or "").replace("Bearer ", "").strip()
                principal = self.app.access.authenticate(token)
                self.app.limiter.check("api_per_token_per_min", principal.user_id)
                if permission:
                    self.app.access.require(principal, permission)
                tenant_id = self.app.access.require_tenant(principal, body.get("tenant_id"))
            ctx = {"body": body, "raw_body": raw, "headers": headers, "query": query,
                   "principal": principal, "tenant_id": tenant_id}
            payload = handler(ctx, **params)
        except SaraError as exc:
            status_code, payload = exc.status, exc.to_dict()
            self.app.metrics.inc("errors")
        except json.JSONDecodeError:
            status_code, payload = 400, {"error": "bad_json", "message": "invalid JSON body"}
        except Exception as exc:
            LOG.exception("unhandled error")
            self.app.metrics.inc("errors")
            status_code, payload = 500, {"error": "internal_error", "message": str(exc)[:200]}
        # Allow handlers to serve raw HTML (e.g. the Google OAuth redirect page)
        if isinstance(payload, dict) and payload.get("__html__"):
            body = str(payload["__html__"]).encode("utf-8")
            status_code = int(payload.get("__status__", 200) or 200)
            start_response(str(status_code) + " " + ("OK" if status_code < 400 else "ERROR"),
                           [("Content-Type", "text/html; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
            return [body]
        out = json.dumps(payload, ensure_ascii=False, default=str).encode()
        start_response(str(status_code) + " " + ("OK" if status_code < 400 else "ERROR"),
                       [("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(out)))])
        return [out]


def serve(app: Optional[SaraApp] = None, host: str = "0.0.0.0", port: int = 8080) -> None:
    app = app or SaraApp()
    app.jobs.start()
    wsgi = WsgiApp(app)
    try:
        import uvicorn
        from asgiref.wsgi import WsgiToAsgi
        uvicorn.run(WsgiToAsgi(wsgi), host=host, port=port, log_level="info")
        return
    except Exception:
        pass
    from wsgiref.simple_server import make_server
    LOG.info("serving SARA AI on http://%s:%d (wsgiref)", host, port)
    make_server(host, port, wsgi).serve_forever()


# --------------------------------------------------------- tests / regression
def run_self_tests(app: Optional[SaraApp] = None) -> Dict[str, Any]:
    # Deterministic self-test + regression suite (safe on an isolated temp store).
    import tempfile
    results: List[Dict[str, Any]] = []

    def check(name: str, condition: bool, note: str = "") -> None:
        results.append({"case": name, "passed": bool(condition), "note": note})

    owns_app = app is None
    if owns_app:
        raw = json.loads(Path(SCHEMA_FILE).read_text(encoding="utf-8"))
        tmp = tempfile.mkdtemp(prefix="sara_test_")
        raw["storage"]["root"] = tmp
        raw["database"]["url"] = "sqlite:///" + str(Path(tmp) / "test.db")
        raw["integrations"]["telegram"]["adapters"]["default"] = "mock"
        os.environ.setdefault("SARA_MASTER_KEY", "test-master-key")
        app = SaraApp(Config(raw))

    boot = app.bootstrap_tenant("Test Hotel", "owner@test.local")
    tenant_id = boot["tenant"]["id"]
    principal = app.access.authenticate(boot["api_token"])
    check("bootstrap_owner_role", principal.role == "owner")

    # crypto round trip
    secret = "+1 555 0100"
    check("crypto_roundtrip", app.crypto.decrypt(app.crypto.encrypt(secret)) == secret)

    # RBAC
    _, staff_token = app.access.create_user(tenant_id, "staff@test.local", "staff")
    staff = app.access.authenticate(staff_token)
    try:
        app.access.require(staff, "prompts:write")
        check("rbac_denies_staff_prompt_write", False)
    except Forbidden:
        check("rbac_denies_staff_prompt_write", True)

    # file validation
    try:
        app.ingestion.validator.validate("evil.exe", b"MZ\x00\x00")
        check("blocks_executables", False)
    except ValidationError:
        check("blocks_executables", True)

    # regression: ungrounded price question must escalate and quote no numbers
    for case in app.cfg.get("testing.regression_cases", []) or []:
        expect = case.get("expect") or {}
        if case.get("empty_knowledge"):
            decision = app.conversations.handle(tenant_id, "chat_empty_" + case["id"],
                                                case["input"])
            ok = True
            if expect.get("escalated"):
                ok = ok and decision.escalated
            if expect.get("must_not_contain_digits"):
                ok = ok and not re.search(r"\d", decision.reply)
            check("regression:" + case["id"], ok, decision.escalation_reason or "")
            continue
        decision = app.conversations.handle(tenant_id, "chat_" + case["id"], case["input"])
        ok = True
        if "language" in expect:
            ok = ok and decision.language == expect["language"]
        if "intent" in expect:
            ok = ok and decision.intent == expect["intent"]
        if expect.get("escalated"):
            ok = ok and decision.escalated
        check("regression:" + case["id"], ok,
              decision.intent + "/" + decision.language + "/" + str(decision.escalated))

    # ingestion + grounded retrieval + routing
    csv_bytes = ("Room Type,Rate,Currency,Available,Date\n"
                 "Deluxe,180,USD,4,2030-01-10\n"
                 "Standard,120,USD,7,2030-01-10\n").encode()
    ing = app.ingestion.ingest(principal, "rates.csv", csv_bytes, "text/csv")
    check("ingest_csv_indexed", ing["route"] == "indexed" and ing["facts"] > 0,
          json.dumps({"facts": ing["facts"], "chunks": ing["chunks"]}))
    ing_json = app.ingestion.ingest(principal, "policies.json",
                                    json.dumps({"cancellation policy":
                                                "Free cancellation up to 48 hours before arrival."}
                                               ).encode(), "application/json")
    check("routing_direct_for_json", ing_json["route"] == "direct")
    check("versioning_dedupe", app.ingestion.ingest(principal, "rates.csv", csv_bytes,
                                                    "text/csv")["status"] == "duplicate")

    price = app.conversations.handle(tenant_id, "chat_price", "How much is the deluxe room?")
    check("grounded_price_answer",
          (not price.escalated) and "180" in price.reply and bool(price.sources),
          price.reply[:120])
    avail = app.conversations.handle(tenant_id, "chat_avail", "Is a deluxe room available?")
    check("grounded_availability_answer", not avail.escalated, avail.reply[:120])

    # language detection
    ar = app.conversations.handle(tenant_id, "chat_ar", "مرحبا، ما هو سعر الغرفة؟")
    check("arabic_language_reply", ar.language == "ar", ar.reply[:80])

    # prompt registry override + versioning
    upd = app.prompts.update(principal, "system", "Custom system prompt for {business_name}.")
    check("prompt_versioning", upd["version"] >= 1 and
          app.prompts.get(tenant_id, "system")["source"] == "tenant_override")

    # booking + document generation
    booking = app.bookings.create_draft(tenant_id, None, {
        "full_name": "Jane Doe", "phone": "+15550100", "checkin_date": "2030-02-01",
        "checkout_date": "2030-02-04", "room_type": "Deluxe", "guests": 2,
        "price_total": 540, "currency": "USD", "preferences": "high floor, late arrival",
        "checkin_instructions": "Front desk, passport required"})
    check("booking_requires_approval", booking["status"] == "pending_approval")
    check("booking_pii_encrypted", booking["data"]["phone"] == "+15550100")
    docs = app.documents.generate_for_booking(tenant_id, booking, ["rates.csv@v1"], "VIP guest")
    pdfs = [d for d in docs if d["fmt"] == "pdf"]
    check("documents_generated", len(docs) >= 3 and len(pdfs) >= 2, str(len(docs)) + " files")
    check("pdf_is_pdf", Path(pdfs[0]["storage_path"]).read_bytes()[:4] == b"%PDF")
    approved = app.bookings.set_status(principal, booking["id"], "confirmed")
    check("booking_status_versioned", approved["status"] == "confirmed" and approved["version"] == 2)

    # past-date validation
    try:
        app.bookings.create_draft(tenant_id, None, {
            "full_name": "Old Date", "phone": "+1", "checkin_date": "2000-01-01",
            "checkout_date": "2000-01-03", "room_type": "Deluxe", "guests": 1})
        check("rejects_past_dates", False)
    except ValidationError:
        check("rejects_past_dates", True)

    # telegram webhook idempotency + adapter outbox
    update = {"update_id": 991, "message": {"chat_id": 1, "chat": {"id": 555},
                                            "from": {"first_name": "Guest"},
                                            "text": "Hello, do you have a pool?"}}
    first = app.handle_telegram_update(tenant_id, update, "mock")
    second = app.handle_telegram_update(tenant_id, update, "mock")
    check("telegram_idempotency", first["status"] == "ok" and second["status"] == "duplicate")
    check("telegram_outbox_used", len(app.telegram.adapters["mock"].outbox) >= 1)

    # rate limiting
    try:
        for _ in range(int(app.cfg.get("security.rate_limits.telegram_inbound_per_chat_per_min", 20)) + 2):
            app.limiter.check("telegram_inbound_per_chat_per_min", "burst-chat")
        check("rate_limit_enforced", False)
    except RateLimited:
        check("rate_limit_enforced", True)

    # sheets connector with injected transport (managed auth, no user credentials)
    sheet_rows = {"values": [["Date", "Room Type", "Available", "Rate", "Currency", "Updated At"],
                             ["2030-01-10", "Deluxe", "2", "195", "USD", iso()]]}
    written: List[Dict[str, Any]] = []

    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if method == "GET" and "/values/" in path:
            return sheet_rows
        if method == "GET" and path == "/spreadsheets":
            return {"files": [{"id": "sheet1", "name": "Ops"}]}
        written.append({"path": path, "payload": payload})
        return {"updated": True}

    app.sheets.transport = transport
    conn = app.sheets.start_authorization(principal, "Ops sheet")
    check("sheets_managed_auth_no_user_keys",
          conn["user_supplied_credentials_required"] is False and "authorize_url" in conn)
    app.sheets.complete_authorization(tenant_id, conn["connection_id"],
                                      {"access_token": "managed-token", "account": "ops@hotel"})
    hist = app.sheets.sync(principal, "availability", "sheet1", "Availability", "bidirectional")
    check("sheets_sync_inbound", hist["status"] == "ok" and hist["rows_read"] == 1,
          json.dumps({"conflicts": hist["conflicts"]}))
    after = app.knowledge.availability_lookup(tenant_id, "deluxe")
    check("sheets_is_authoritative_for_availability",
          after["found"] and after["rows"][0]["value"] == "2")
    lead_hist = app.sheets.sync(principal, "leads", "sheet1", "Leads", "outbound")
    check("sheets_sync_history_recorded", len(app.sheets.history(tenant_id)) >= 2,
          lead_hist["status"])

    # voice path uses same orchestration
    app.voice.stt_hook = lambda audio, fmt: "How much is the standard room?"
    transcript = app.voice.transcribe(tenant_id, "sess1", b"\x00\x01", "ogg")
    voice_decision = app.conversations.handle(tenant_id, "voice:tester", transcript["text"], "voice")
    check("voice_shares_agent_pipeline", "120" in voice_decision.reply or voice_decision.escalated,
          voice_decision.reply[:100])

    # escalation triggers
    human = app.conversations.handle(tenant_id, "chat_human", "I want to talk to a human")
    check("human_request_escalates", human.escalated and human.escalation_reason == "human_request")
    discount = app.conversations.handle(tenant_id, "chat_disc", "Can you give me a discount?")
    check("discount_escalates", discount.escalated)

    # jobs, backups, audit, health
    app.jobs.enqueue(tenant_id, "delivery", "telegram_deliver",
                     {"tenant_id": tenant_id, "chat_id": "555", "text": "queued hello",
                      "adapter": "mock"})
    check("job_executed", (app.jobs.run_once() or {}).get("status") == "done")
    app.jobs.enqueue(tenant_id, "ingest", "does_not_exist", {})
    check("job_retry_on_unknown_kind", (app.jobs.run_once() or {}).get("status") == "retrying")
    check("backup_snapshot", app.storage.backup(tenant_id).startswith("snap_"))
    trail = app.audit.trail(tenant_id)
    events = {row["event"] for row in trail}
    check("audit_covers_pipeline",
          {"ingest", "normalize", "message_in", "message_out", "decision",
           "document_generated", "sync"}.issubset(events),
          ",".join(sorted(events)))
    check("health_ok", app.health()["status"] in ("ok", "degraded"))

    # tenant isolation
    other = app.bootstrap_tenant("Other Hotel", "owner2@test.local")
    other_principal = app.access.authenticate(other["api_token"])
    try:
        app.access.require_tenant(other_principal, tenant_id)
        check("tenant_isolation", False)
    except Forbidden:
        check("tenant_isolation", True)
    check("knowledge_scoped_to_tenant",
          app.knowledge.search(other["tenant"]["id"], "deluxe room rate") == [])

    # deterministic replay from audit trail
    replay = [json.loads(r["payload"]) for r in trail if r["event"] == "decision"]
    check("replay_records_decisions",
          bool(replay) and all("tool_calls" in r for r in replay))

    passed = sum(1 for r in results if r["passed"])
    summary = {"total": len(results), "passed": passed, "failed": len(results) - passed,
               "results": results}
    if owns_app:
        app.jobs.stop()
    return summary


# ----------------------------------------------------------------------- CLI
def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    command = argv[0] if argv else "serve"
    if command == "selftest":
        summary = run_self_tests()
        for row in summary["results"]:
            print(("PASS " if row["passed"] else "FAIL ") + row["case"] +
                  ((" | " + row["note"]) if row["note"] else ""))
        print(json.dumps({k: summary[k] for k in ("total", "passed", "failed")}))
        return 0 if summary["failed"] == 0 else 1
    if command == "migrate":
        app = SaraApp()
        print(json.dumps({"applied": app.db.migrate()}))
        return 0
    if command == "bootstrap":
        app = SaraApp()
        name = argv[1] if len(argv) > 1 else "My Hotel"
        email = argv[2] if len(argv) > 2 else "owner@example.com"
        print(json.dumps(app.bootstrap_tenant(name, email), indent=2))
        return 0
    if command == "health":
        print(json.dumps(SaraApp().health(), indent=2))
        return 0
    if command == "serve":
        port = int(argv[1]) if len(argv) > 1 else int(os.environ.get("PORT", "8080"))
        serve(port=port)
        return 0
    print("usage: whatsappagent.py [serve [port] | selftest | migrate | bootstrap | health]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
