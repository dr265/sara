"""Connected-device registry and secure phone/desktop pairing.

Ultron runs on the backend and the machine it controls runs on someone's desk.
Something has to join the two, and that something has to be authenticated,
time-bounded and revocable - otherwise "connect your phone" is a public
endpoint with a keyboard on it.

The flow implemented here is the standard one:

    ULTRON -> POST /ultron/pairing       -> a short-lived pairing token + QR
    PHONE  -> POST /ultron/pairing/claim -> redeems it, receives a device token
    PHONE  -> WS   /ultron/link?token=   -> heartbeats, streams realtime events
    ULTRON -> POST /ultron/devices/{id}/revoke -> cut it off immediately

Security properties this module is responsible for:

* **Tokens are stored hashed.** Only ``sha256`` digests live in this file and
  in memory; a raw token exists only in the response that mints it. A heap dump
  of the backend therefore yields no usable credential.
* **Pairing tokens are single-use and expire.** ``PAIRING_TTL_S`` (two
  minutes) is short on purpose: the token is only meant to survive the few
  seconds it takes to point a phone camera at a QR code.
* **Comparison is constant-time.** ``hmac.compare_digest`` everywhere, so a
  wrong token cannot be found one character at a time by timing.
* **Brute force is throttled.** Claiming is the only unauthenticated write in
  the flow, so failed attempts are counted per remote address and locked out.
* **Desktop actions are allowlisted per device.** ``DEVICE_SCOPES`` is the
  complete set of things a paired device may ask the backend to do. A phone
  gets voice and read scopes; it never gets ``computer.*``.

Connection state is *derived*, never asserted: a device is ``online`` only
while it is actually heartbeating within :data:`ONLINE_S`, ``stale`` after that
until :data:`STALE_S`, and ``offline`` beyond it. A device that crashes keeps
its row and simply decays, which is what a reconnecting client needs to see.

Scope, deliberately: the registry is **in-process and in-memory**. It does not
survive a backend restart, and this docstring says so rather than implying
otherwise. Persistence belongs in the session/memory stores, which already
have a defined on-disk format.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .tenancy import LOCAL_TENANT_ID

log = logging.getLogger("sara.ultron.devices")

# ---------------------------------------------------------------------------
# Timing and policy constants
# ---------------------------------------------------------------------------

#: How long a pairing token stays claimable. The user is scanning a QR code.
PAIRING_TTL_S = 120

#: A heartbeat inside this window means the device is genuinely connected.
ONLINE_S = 20.0

#: Past ONLINE_S but inside this window: connected before, quiet now. This is
#: the state a reconnecting client looks for.
STALE_S = 90.0

#: Retained after a device has been offline this long, so history is visible.
OFFLINE_RETENTION_S = 24 * 3600.0

#: Failed claim attempts allowed per address before a temporary lockout.
CLAIM_ATTEMPTS = 8

#: Length of the lockout once the attempts are exhausted.
CLAIM_LOCKOUT_S = 120.0

def _default_state_path() -> str:
    """Where the paired-device registry is persisted.

    The registry used to live only in memory, which meant every backend
    restart silently unpaired every phone and desktop and the command center
    went back to demanding "CONNECT DESKTOP". Tokens are stored HASHED (never
    in the clear), so this file is not a credential store: reading it grants no
    access, and losing it only means the devices re-pair.
    """
    override = os.getenv("SARA_DEVICE_STATE_PATH", "").strip()
    if override:
        return override
    base = os.getenv("SARA_STATE_DIR", "").strip() or os.path.join(
        os.path.expanduser("~"), ".sara")
    return os.path.join(base, "devices.json")


STATE_PATH = _default_state_path()

#: Longest accepted device name / platform string (echoed into the UI).
LABEL_LIMIT = 64

#: The complete set of things a paired device may do. Nothing outside this set
#: is reachable through a device token.
#:
#: Every scope here maps to an endpoint that exists in ``api.py`` today. No
#: ``computer.*`` scope is declared, because this build ships no path from a
#: device token to the machine's input stack - declaring one would put a
#: permission on the list that nothing enforces, which is worse than omitting
#: it. The desktop agent's own API (``app/api/desktop_agent.py``) remains the
#: only way to drive the desktop, behind its own ``FULLAI_API_TOKEN``.
DEVICE_SCOPES: Dict[str, Tuple[str, ...]] = {
    "phone": (
        "voice.listen",
        "voice.speak",
        "voice.cancel",
        "task.read",
        "task.submit",
        "device.read",
    ),
    "tablet": (
        "voice.listen",
        "voice.speak",
        "voice.cancel",
        "task.read",
        "task.submit",
        "device.read",
    ),
    "desktop": (
        "task.read",
        "task.submit",
        "device.read",
    ),
    # The machine that actually drives the desktop. Separate from "desktop",
    # which is a viewing surface: a tablet can be a desktop device (read
    # tasks, stream events) without being trusted to move the mouse.
    #
    # pc.execute is the scope that makes a link an EXECUTOR. Without it the
    # backend never dispatches a computer_control / file_controller /
    # system_command call to that socket, so a stolen viewer token cannot
    # become a remote-control credential.
    "pc_agent": (
        "task.read",
        "task.submit",
        "device.read",
        "pc.execute",
        "pc.context",
    ),
}

#: Device kinds that may be paired at all.
DEVICE_KINDS = tuple(DEVICE_SCOPES)

#: The complete vocabulary of scopes the system implements.
#:
#: Every entry must appear in at least one kind above, and every scope a kind
#: declares must appear here. Keeping the vocabulary separate from any single
#: kind is what stopped ``pc_agent``'s executor scopes from looking like typos:
#: the invariant being guarded is "no scope is declared that nothing implements",
#: not "every scope belongs to a phone or a viewer".
KNOWN_SCOPES = frozenset({
    "voice.listen",
    "voice.speak",
    "voice.cancel",
    "task.read",
    "task.submit",
    "device.read",
    # Only a paired Windows machine gets these. They are what make a device an
    # executor rather than a viewer, so they are never granted to a phone.
    "pc.execute",
    "pc.context",
})

#: Connection states. ``online``/``stale``/``offline`` are derived from the last
#: heartbeat; ``revoked`` is sticky.
STATE_ONLINE = "online"
STATE_STALE = "stale"
STATE_OFFLINE = "offline"
STATE_REVOKED = "revoked"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass
class PairingOffer:
    """A minted, unclaimed pairing token.

    Note what is *not* on this dataclass: the raw token. Only
    :attr:`token_hash` is retained, so an unclaimed pairing secret never sits
    in the registry's memory after the response that returned it has been sent.
    (An earlier revision did keep it, which the regression suite caught -
    ``test_raw_token_is_never_retained``.)
    """

    token_hash: str
    kind: str
    label: str
    created: float
    expires_at: float
    pair_code: str

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def payload(self, backend_url: str, token: str) -> str:
        """The exact string a QR code encodes.

        Kept short on purpose: it has to fit a scannable QR at the default
        error-correction level, so it carries the backend URL and the token and
        nothing else. No name, no platform, no scope list.

        ``token`` is passed in rather than read from the object precisely
        because the object does not hold it.
        """
        base = (backend_url or "").rstrip("/")
        return f"{base}/ultron/pair/claim?t={token}"


@dataclass
class Device:
    """One paired device."""

    device_id: str
    kind: str
    label: str
    platform: str
    token_hash: str
    scopes: List[str]
    created: float
    last_seen: float
    #: Which tenant paired this device. Defaults to the local operator so every
    #: existing install keeps working untouched, and so no record can exist
    #: without an owner.
    owner_id: str = LOCAL_TENANT_ID
    connected: bool = False
    revoked: bool = False
    revoked_reason: str = ""
    client_info: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def state(self, now: Optional[float] = None) -> str:
        now = now if now is not None else time.time()
        if self.revoked:
            return STATE_REVOKED
        if now - self.created > OFFLINE_RETENTION_S:
            return STATE_OFFLINE
        age = now - self.last_seen
        if age <= ONLINE_S:
            return STATE_ONLINE
        if age <= STALE_S:
            return STATE_STALE
        return STATE_OFFLINE

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def as_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Public view. Never contains the token or its hash."""
        now = now if now is not None else time.time()
        state = self.state(now)
        return {
            "device_id": self.device_id,
            "owner_id": self.owner_id,
            "kind": self.kind,
            "label": self.label,
            "platform": self.platform,
            "scopes": list(self.scopes),
            "state": state,
            "connected": state == STATE_ONLINE,
            "created": self.created,
            "last_seen": self.last_seen,
            "last_seen_age_s": round(max(0.0, now - self.last_seen), 1),
            "revoked": self.revoked,
            "revoked_reason": self.revoked_reason,
            "note": self.note,
            "client": {
                key: value
                for key, value in self.client_info.items()
                if key in ("app", "version", "remote")
            },
        }


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------
class _Throttle:
    """Per-address failed-attempt counter with a lockout window."""

    def __init__(self, limit: int = CLAIM_ATTEMPTS, lockout_s: float = CLAIM_LOCKOUT_S):
        self._limit = limit
        self._lockout_s = lockout_s
        self._fails: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def check(self, address: str) -> Optional[float]:
        """Seconds remaining on the lockout, or ``None`` when clear."""
        now = time.time()
        with self._lock:
            hits = [t for t in self._fails.get(address, []) if now - t < self._lockout_s]
            self._fails[address] = hits
            if len(hits) >= self._limit:
                return round(self._lockout_s - (now - hits[0]), 1)

    def record_failure(self, address: str) -> int:
        now = time.time()
        with self._lock:
            hits = [t for t in self._fails.get(address, []) if now - t < self._lockout_s]
            hits.append(now)
            self._fails[address] = hits
            return len(hits)

    def clear(self, address: str) -> None:
        with self._lock:
            self._fails.pop(address, None)


# ---------------------------------------------------------------------------
# QR rendering (optional dependency)
# ---------------------------------------------------------------------------
def render_qr(text: str) -> Dict[str, Any]:
    """Encode ``text`` as a QR PNG data URL.

    ``segno`` is a pure-Python QR encoder with no transitive dependencies. It is
    imported lazily and its absence is *reported*, not hidden: a pairing flow
    that cannot draw a QR still returns the redeem code, and the UI shows it as
    text instead of pretending a QR was produced.
    """
    try:
        import io as _io

        import segno
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "data_url": None,
                "error": f"QR encoder unavailable: {type(exc).__name__}: {exc}"}
    try:
        buffer = _io.BytesIO()
        segno.make(text, error="m").save(buffer, kind="png", scale=6, border=2,
                                        dark="#000000", light="#ffffff")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return {"available": True, "data_url": f"data:image/png;base64,{encoded}",
                "error": None}
    except Exception as exc:  # noqa: BLE001 - never break pairing over a QR
        log.warning("QR rendering failed: %s", exc)
        return {"available": False, "data_url": None,
                "error": f"QR rendering failed: {type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class DeviceRegistry:
    """Thread-safe registry of paired devices and outstanding pairing offers."""

    def __init__(self, state_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._devices: Dict[str, Device] = {}
        self._offers: Dict[str, PairingOffer] = {}
        self._throttle = _Throttle()
        self._state_path = state_path or STATE_PATH
        # Offers are deliberately NOT persisted: a pairing token is single-use
        # and expires in two minutes, so surviving a restart would only widen
        # the window on a credential that is meant to be momentary.
        self._load()

    # -- persistence ----------------------------------------------------
    def _load(self) -> None:
        """Restore paired devices from disk.

        Best-effort by design: a missing, unreadable or corrupt file must never
        stop the backend from booting. The cost of failing here is that the user
        re-pairs, which is exactly what happened before this existed.
        """
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Device state unreadable at %s (%s); starting empty.",
                        self._state_path, type(exc).__name__)
            return

        devices = blob.get("devices") if isinstance(blob, dict) else None
        if not isinstance(devices, list):
            return
        restored = 0
        for row in devices:
            try:
                device = Device(
                    device_id=str(row["device_id"]),
                    kind=str(row["kind"]),
                    label=str(row["label"]),
                    platform=str(row.get("platform", "")),
                    token_hash=str(row["token_hash"]),
                    scopes=[str(s) for s in row.get("scopes", [])],
                    created=float(row["created"]),
                    last_seen=float(row["last_seen"]),
                    # Rows written before tenant isolation existed carry no
                    # owner; they belong to the operator, which is who created
                    # them.
                    owner_id=str(row.get("owner_id") or LOCAL_TENANT_ID),
                    connected=False,  # never trust a persisted "connected"
                    revoked=bool(row.get("revoked", False)),
                    revoked_reason=str(row.get("revoked_reason", "")),
                    client_info=dict(row.get("client_info", {})),
                    note=str(row.get("note", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            # A token hash that is not a hex sha256 cannot have been issued by
            # this code; refuse it rather than let it authenticate anything.
            if not re.fullmatch(r"[0-9a-f]{64}", device.token_hash):
                continue
            if device.kind not in DEVICE_SCOPES:
                continue
            self._devices[device.device_id] = device
            restored += 1
        if restored:
            log.info("Restored %d paired device(s) from %s",
                     restored, self._state_path)

    def _save(self) -> None:
        """Write the registry atomically. Never raises.

        Write-to-temp-then-``os.replace`` so a crash mid-write cannot leave a
        truncated file that would wipe every pairing on the next boot.
        """
        with self._lock:
            rows = [
                {
                    "device_id": d.device_id, "kind": d.kind, "label": d.label,
                    "platform": d.platform, "token_hash": d.token_hash,
                    "scopes": list(d.scopes), "created": d.created,
                    "last_seen": d.last_seen, "revoked": d.revoked,
                    "owner_id": d.owner_id,
                    "revoked_reason": d.revoked_reason,
                    "client_info": d.client_info, "note": d.note,
                }
                for d in self._devices.values()
            ]
        blob = {"version": 1, "devices": rows}
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            directory = os.path.dirname(self._state_path) or "."
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".devices-",
                                       suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(blob, fh)
                os.replace(tmp, self._state_path)
            except BaseException:
                # Never leave the temp file behind on a failed write.
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            log.warning("Could not persist device state to %s (%s). "
                        "Pairings will not survive a restart.",
                        self._state_path, exc)

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _clean(value: Any, limit: int = LABEL_LIMIT) -> str:
        """Strip control characters and clamp a client-supplied label."""
        text = "".join(ch for ch in str(value or "") if ch.isprintable())
        return text.strip()[:limit]

    def _purge(self, now: float) -> None:
        for offer_id in [
            oid for oid, offer in self._offers.items()
            if offer.expires_at + 300 < now
        ]:
            self._offers.pop(offer_id, None)
        for device_id in [
            did for did, device in self._devices.items()
            if device.revoked and now - device.last_seen > OFFLINE_RETENTION_S
        ]:
            self._devices.pop(device_id, None)

    # -- pairing -------------------------------------------------------
    def create_offer(self, kind: str = "phone", label: str = "",
                     backend_url: str = "", ttl_s: int = PAIRING_TTL_S) -> Dict[str, Any]:
        """Mint a single-use pairing token and its QR code.

        Returns the raw token exactly once. It cannot be recovered afterwards -
        only its hash is kept.
        """
        kind = (kind or "phone").strip().lower()
        if kind not in DEVICE_KINDS:
            raise ValueError(
                f"Unknown device kind {kind!r}. Use one of: {', '.join(DEVICE_KINDS)}."
            )
        ttl_s = max(15, min(int(ttl_s or PAIRING_TTL_S), 900))
        token = secrets.token_urlsafe(24)
        # A short human-typable code alongside the token, for the case where the
        # camera will not focus. It is only ever *displayed*; the token stays
        # the thing that is verified.
        pair_code = f"{secrets.randbelow(1_000_000):06d}"
        now = time.time()
        offer = PairingOffer(
            token_hash=self._hash(token),
            kind=kind,
            label=self._clean(label) or f"New {kind}",
            created=now,
            expires_at=now + ttl_s,
            pair_code=pair_code,
        )
        payload = offer.payload(backend_url, token)
        qr = render_qr(payload)
        with self._lock:
            self._purge(now)
            self._offers[offer.token_hash] = offer
        log.info("Pairing offer created for %s (ttl=%ss)", kind, ttl_s)
        return {
            "token": token,
            "pair_code": offer.pair_code,
            "kind": kind,
            "label": offer.label,
            "payload": payload,
            "expires_at": offer.expires_at,
            "expires_in_s": ttl_s,
            "qr": qr,
            "note": (
                "This token is shown once and expires in "
                f"{ttl_s}s. Redeem it on the device to finish pairing."
            ),
        }

    def auto_pair(self, kind: str, label: str = "", platform: str = "",
                  address: str = "loopback", client_info: Optional[Dict[str, Any]] = None,
                  owner_id: str = LOCAL_TENANT_ID,
                  ) -> Dict[str, Any]:
        """Pair a device that is already on this machine, with no human step.

        Idempotent by construction: if a non-revoked device of the same kind
        already exists it is REUSED rather than duplicated, and its token is
        rotated so the caller always receives a usable one. Without the
        rotation this method could only ever help the very first run, and every
        later run - after a cache wipe, a moved checkout, a deleted state file -
        would dead-end with "already paired" and no way forward.

        Deliberately callable only from a loopback/private address; see
        ``api.py`` for the guard. This is the difference between "the operator
        already owns this machine" and "someone on the internet asked nicely".
        """
        kind = (kind or "pc_agent").strip().lower()
        if kind not in DEVICE_SCOPES:
            raise ValueError(
                f"Unknown device kind {kind!r}. Use one of: {', '.join(DEVICE_KINDS)}."
            )
        now = time.time()
        with self._lock:
            existing = next(
                (d for d in self._devices.values()
                 if d.kind == kind and not d.revoked
                 and d.owner_id == str(owner_id or LOCAL_TENANT_ID)), None)

            if existing is not None and existing.connected:
                # A live session is using this identity. Rotating its token now
                # would knock an agent that is already working off the link, so
                # refuse rather than fix a problem the caller may not have.
                return {
                    "device_token": "",
                    "device": existing.as_dict(now),
                    "reused": True,
                    "rotated": False,
                    "note": ("This machine is already connected. Its agent is "
                             "running now and will refresh its own token from "
                             "its local cache; nothing to do."),
                }

            device_token = secrets.token_urlsafe(32)
            if existing is not None:
                # Re-pair the SAME device rather than creating a second one, and
                # rotate its token. Refusing here (because only the hash was
                # kept) turned a lost token cache into a permanent lockout -
                # exactly the "please connect the desktop" loop this endpoint
                # exists to end. The old token stops working, which is the
                # correct outcome for a re-pair on the operator's own machine.
                existing.token_hash = self._hash(device_token)
                existing.last_seen = now
                existing.connected = False
                if label:
                    existing.label = self._clean(label)
                if platform:
                    existing.platform = self._clean(platform, 32)
                existing.client_info = {"app": "sara-local-agent",
                                        "remote": self._clean(address, 64)}
                self._save()
                log.info("Re-paired local %s: %s (%s)", existing.kind,
                         existing.device_id, existing.label)
                return {"device_token": device_token,
                        "device": existing.as_dict(now),
                        "reused": True, "rotated": True}

            device = Device(
                device_id=f"dev_{secrets.token_hex(4)}",
                kind=kind,
                label=self._clean(label) or f"Local {kind}",
                platform=self._clean(platform, 32),
                token_hash=self._hash(device_token),
                scopes=list(DEVICE_SCOPES[kind]),
                created=now,
                last_seen=now,
                owner_id=str(owner_id or LOCAL_TENANT_ID),
                client_info={"app": "sara-local-agent",
                             "remote": self._clean(address, 64)},
            )
            self._devices[device.device_id] = device
        self._save()
        log.info("Auto-paired local %s: %s (%s)", device.kind, device.device_id,
                 device.label)
        return {"device_token": device_token, "device": device.as_dict(),
                "reused": False, "rotated": False}

    def claim(self, token: str, kind: str = "phone", label: str = "",
              platform: str = "", address: str = "unknown",
              owner_id: str = LOCAL_TENANT_ID) -> Dict[str, Any]:
        """Redeem a pairing token for a long-lived device token.

        Raises ``PermissionError`` for every failure - unknown, expired,
        already-used, revoked - and ``ValueError`` only for a malformed request,
        so the caller cannot tell from the response which tokens exist.
        """
        remaining = self._throttle.check(address)
        if remaining is not None:
            raise PermissionError(
                f"Too many failed pairing attempts. Try again in {remaining:.0f}s."
            )

        token = (token or "").strip()
        token_hash = self._hash(token) if token else ""
        now = time.time()
        with self._lock:
            self._purge(now)
            offer = self._offers.get(token_hash)
            valid = offer is not None and not offer.expired
            if valid and offer.kind != kind:
                # The QR was minted for a different device kind; honour what was
                # actually issued rather than silently widening the scopes.
                valid = False
            if not valid:
                fails = self._throttle.record_failure(address)
                raise PermissionError("Pairing token is invalid, expired or already used.")
            del self._offers[token_hash]
            device_token = secrets.token_urlsafe(32)
            device = Device(
                    device_id=f"dev_{secrets.token_hex(4)}",
                    kind=offer.kind,
                    label=self._clean(label) or offer.label,
                    platform=self._clean(platform, 32),
                    token_hash=self._hash(device_token),
                    scopes=list(DEVICE_SCOPES.get(offer.kind, ())),
                    created=now,
                    last_seen=now,
                    owner_id=str(owner_id or LOCAL_TENANT_ID),
                    client_info={"app": "sara-ultron-client", "remote": address},
                )
            self._devices[device.device_id] = device
        self._save()
        self._throttle.clear(address)
        log.info("Device paired: %s (%s, %s)", device.device_id, device.kind, device.label)
        return {"device_token": device_token, "device": device.as_dict()}

    # -- authentication ------------------------------------------------
    def authenticate(self, token: str,
                      owner_id: Optional[str] = None) -> Optional[Device]:
        """Resolve a device token to its device, or ``None``.

        ``owner_id`` additionally requires the device to belong to that tenant.
        Authentication and authorisation are deliberately one call here: a valid
        token for ANOTHER tenant's device must not resolve, or a leaked token
        would be a working credential for someone else's computer.

        A revoked device is never returned, even with a valid token: revocation
        has to actually cut the connection, not merely mark it.
        """
        token = (token or "").strip()
        if not token:
            return None
        digest = self._hash(token)
        with self._lock:
            for device in self._devices.values():
                if hmac.compare_digest(device.token_hash, digest):
                    if device.revoked:
                        return None
                    # A token for another tenant's device must not resolve,
                    # even though the signature matched.
                    if owner_id is not None and device.owner_id != owner_id:
                        return None
                    device.last_seen = time.time()
                    return device
        return None

    def authenticate_scoped(self, token: str, scope: str,
                             owner_id: Optional[str] = None) -> Optional[Device]:
        device = self.authenticate(token, owner_id=owner_id)
        if device is None or not device.has(scope):
            return None
        return device

    # -- lifecycle -----------------------------------------------------
    def heartbeat(self, device_id: str, info: Optional[Dict[str, Any]] = None) -> Optional[Device]:
        """Record that a device is still there. Returns the updated device."""
        with self._lock:
            device = self._devices.get(device_id)
            if device is None or device.revoked:
                return None
            device.last_seen = time.time()
            if info:
                # Only a fixed allowlist of client-supplied keys is retained, so
                # a device cannot push arbitrary structure into the UI.
                for key in ("app", "version", "remote", "focus"):
                    value = info.get(key)
                    if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 120:
                        device.client_info[key] = value
        self._save()
        return device

    def set_connected(self, device_id: str, connected: bool) -> None:
        with self._lock:
            device = self._devices.get(device_id)
            if device is not None:
                device.connected = connected
                device.last_seen = time.time() if connected else device.last_seen

    def revoke(self, device_id: str, reason: str = "revoked by operator",
               owner_id: Optional[str] = None) -> bool:
        """Revoke one device. A tenant mismatch reads as "not found".

        ``owner_id`` follows the same isolation rule as :meth:`get` and
        :meth:`list`: the route that calls this passes the caller's tenant, and
        a device owned by somebody else answers exactly like a missing one, so
        this cannot be used to probe which device ids exist. It is optional
        only so the single-operator local install and the existing tests keep
        working.
        """
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                return False
            if owner_id is not None and device.owner_id != owner_id:
                return False
            device.revoked = True
            device.connected = False
            device.revoked_reason = self._clean(reason, 160)
        self._save()
        return True

    def revoke_all(self, reason: str = "global stop") -> int:
        with self._lock:
            count = 0
            for device in self._devices.values():
                if not device.revoked:
                    device.revoked = True
                    device.connected = False
                    device.revoked_reason = self._clean(reason, 160)
                    count += 1
            self._offers.clear()
        self._save()
        return count

    def get(self, device_id: str, owner_id: Optional[str] = None) -> Optional[Device]:
        """Fetch one device, optionally restricted to a tenant.

        A tenant mismatch reads as "not found" rather than "forbidden", so this
        cannot be used to probe which device ids exist.
        """
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                return None
            if owner_id is not None and device.owner_id != owner_id:
                return None
            return device

    def list(self, kind: Optional[str] = None,
             owner_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Public views of devices, online first, then newest.

        ``owner_id`` is the isolation boundary: a tenant must always pass it. It
        is optional only so the single-operator local install and the existing
        tests keep working - every API route passes it.
        """
        now = time.time()
        with self._lock:
            devices = [d for d in self._devices.values()
                       if owner_id is None or d.owner_id == owner_id]
        views = [d.as_dict(now) for d in devices if kind is None or d.kind == kind]
        order = {STATE_ONLINE: 0, STATE_STALE: 1, STATE_OFFLINE: 2, STATE_REVOKED: 3}
        views.sort(key=lambda v: (order.get(v["state"], 9), -v["created"]))
        return views

    def summary(self, owner_id: Optional[str] = None) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            devices = [d for d in self._devices.values()
                       if owner_id is None or d.owner_id == owner_id]
            offers = [o for o in self._offers.values() if not o.expired]
        counts: Dict[str, int] = {STATE_ONLINE: 0, STATE_STALE: 0,
                                  STATE_OFFLINE: 0, STATE_REVOKED: 0}
        kinds: Dict[str, int] = {}
        for device in devices:
            counts[device.state(now)] = counts.get(device.state(now), 0) + 1
            kinds[device.kind] = kinds.get(device.kind, 0) + 1
        return {
            "total": len(devices),
            "pending_offers": len(offers),
            "by_state": counts,
            "by_kind": kinds,
            "checked_at": now,
        }

    def pending_offer(self, token: str) -> Optional[Dict[str, Any]]:
        """Safe view of an unredeemed offer, for the redeem page's countdown."""
        token = (token or "").strip()
        if not token:
            return None
        with self._lock:
            offer = self._offers.get(self._hash(token))
        if offer is None:
            return None
        return {
            "kind": offer.kind,
            "label": offer.label,
            "expires_at": offer.expires_at,
            "expires_in_s": max(0, round(offer.expires_at - time.time())),
        }

    def clear(self) -> None:
        with self._lock:
            self._devices.clear()
            self._offers.clear()
        self._save()


#: Process-wide registry shared by the API routes and the WebSocket handler.
registry = DeviceRegistry()
