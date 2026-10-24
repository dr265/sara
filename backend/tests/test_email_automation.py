"""Email Automation — sender connection + attachments.

Hermetic: the agent DB and attachment directory are rebound to a tmp_path per
test; SMTP logins are faked at the ``_smtp_login`` seam (no network); the API
is mounted onto a bare FastAPI app so the real app's lifespan never runs.
"""
from __future__ import annotations

import asyncio
import base64
import smtplib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents import email_automation as ea
from app.api.deps import API_KEY

HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch):
    """Point the agent's engine, session factory and file store at tmp_path."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ea.db'}", connect_args={"check_same_thread": False}
    )
    ea.Base.metadata.create_all(engine)
    monkeypatch.setattr(ea, "_engine", engine)
    monkeypatch.setattr(
        ea, "_SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    monkeypatch.setattr(ea, "ATTACHMENT_DIR", att_dir)
    return tmp_path


@pytest.fixture()
def client(isolated):
    app = FastAPI()
    app.include_router(ea.router)
    return TestClient(app)


def _create_campaign(client: TestClient, **overrides) -> str:
    payload = {"name": "Attachments test", **overrides}
    res = client.post("/email-agent/campaigns", headers=HEADERS, json=payload)
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _upload(client: TestClient, campaign_id: str, filename: str, data: bytes):
    return client.post(
        f"/email-agent/campaigns/{campaign_id}/attachments",
        headers=HEADERS,
        files={"file": (filename, data, "application/octet-stream")},
    )


# ---------------------------------------------------------------------------
# Attachments — API lifecycle
# ---------------------------------------------------------------------------


def test_attachment_lifecycle(client: TestClient, isolated: Path):
    cid = _create_campaign(client)
    pdf = b"%PDF-1.4\n% resume body\n"
    res = _upload(client, cid, "Resume.pdf", pdf)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["filename"] == "Resume.pdf"
    assert body["size"] == len(pdf)

    listing = client.get(f"/email-agent/campaigns/{cid}/attachments").json()
    assert [a["filename"] for a in listing] == ["Resume.pdf"]

    dl = client.get(f"/email-agent/campaigns/{cid}/attachments/{body['id']}/download")
    assert dl.status_code == 200
    assert dl.content == pdf

    # Uploading the same filename again replaces the old version (one copy sent).
    res2 = _upload(client, cid, "resume.PDF", pdf + b"v2")
    assert res2.status_code == 200, res2.text
    listing2 = client.get(f"/email-agent/campaigns/{cid}/attachments").json()
    assert len(listing2) == 1
    assert listing2[0]["size"] == len(pdf) + 2

    deleted = client.delete(
        f"/email-agent/campaigns/{cid}/attachments/{listing2[0]['id']}", headers=HEADERS
    )
    assert deleted.status_code == 200
    assert client.get(f"/email-agent/campaigns/{cid}/attachments").json() == []
    assert not any((isolated / "attachments" / cid).glob("*"))


def test_attachment_rejects_bad_type_and_size(
    client: TestClient, monkeypatch
):
    cid = _create_campaign(client)
    bad = _upload(client, cid, "malware.exe", b"MZ fake")
    assert bad.status_code == 400
    assert "Unsupported file type" in bad.json()["detail"]

    monkeypatch.setattr(ea, "MAX_ATTACHMENT_BYTES", 5)
    big = _upload(client, cid, "big.pdf", b"123456")
    assert big.status_code == 400
    assert "per-file limit" in big.json()["detail"]

    monkeypatch.setattr(ea, "MAX_ATTACHMENT_BYTES", 100)
    monkeypatch.setattr(ea, "MAX_ATTACHMENTS_TOTAL_BYTES", 10)
    ok = _upload(client, cid, "one.pdf", b"123456")
    assert ok.status_code == 200
    over = _upload(client, cid, "two.pdf", b"123456")
    assert over.status_code == 400
    assert "total limit" in over.json()["detail"]


def test_delete_campaign_removes_attachment_files(client: TestClient, isolated: Path):
    cid = _create_campaign(client)
    assert _upload(client, cid, "Resume.pdf", b"%PDF-1.4 r").status_code == 200
    assert (isolated / "attachments" / cid).exists()

    deleted = client.delete(f"/email-agent/campaigns/{cid}", headers=HEADERS)
    assert deleted.status_code == 200
    assert not (isolated / "attachments" / cid).exists()
    assert client.get(f"/email-agent/campaigns/{cid}/attachments").status_code == 404


# ---------------------------------------------------------------------------
# Attachments — every message carries them
# ---------------------------------------------------------------------------


def test_build_smtp_message_includes_attachments(tmp_path: Path):
    path = tmp_path / "Resume.pdf"
    path.write_bytes(b"hello-pdf")
    msg = ea._build_smtp_message(
        {"host": "h", "port": "587", "user": "u", "password": "p",
         "from": "a@b.c", "name": "N", "ssl": "false"},
        "to@x.y", "Subject", "Body",
        [{"path": str(path), "filename": "Resume.pdf",
          "mime": "application/pdf", "size": 9}],
    )
    parts = [p for p in msg.walk() if p.get_content_disposition() == "attachment"]
    assert len(parts) == 1
    assert parts[0].get_filename() == "Resume.pdf"
    assert base64.b64decode(parts[0].get_payload()) == b"hello-pdf"


def test_build_gmail_message_includes_attachments(tmp_path: Path):
    path = tmp_path / "Resume.pdf"
    path.write_bytes(b"hello-pdf")
    msg = ea._build_gmail_message(
        "to@x.y", "Subject", "Body",
        [{"path": str(path), "filename": "Resume.pdf",
          "mime": "application/pdf", "size": 9}],
    )
    atts = list(msg.iter_attachments())
    assert len(atts) == 1
    assert atts[0].get_filename() == "Resume.pdf"
    assert atts[0].get_payload(decode=True) == b"hello-pdf"


def test_send_email_loads_campaign_attachments(client: TestClient, isolated: Path, monkeypatch):
    cid = _create_campaign(client)
    assert _upload(client, cid, "Resume.pdf", b"%PDF-1.4 resume").status_code == 200

    captured = {}

    def fake_send(cfg, recipient, subject, body, attachments=()):
        captured["attachments"] = attachments
        return "<fake-id@sara.local>"

    monkeypatch.setattr(ea, "_send_via_smtp", fake_send)
    monkeypatch.setattr(
        ea, "_smtp_config",
        lambda provider: {"host": "h", "port": "587", "user": "u", "password": "p",
                          "from": "a@b.c", "name": "N", "ssl": "false"},
    )

    with ea._db() as db:
        campaign = db.get(ea.Campaign, cid)
    message_id = asyncio.run(ea.send_email(campaign, "to@x.y", "Subject", "Body"))

    assert message_id == "<fake-id@sara.local>"
    assert [a["filename"] for a in captured["attachments"]] == ["Resume.pdf"]
    assert Path(captured["attachments"][0]["path"]).read_bytes() == b"%PDF-1.4 resume"


def test_send_guard_rejects_oversize_totals(client: TestClient, monkeypatch):
    cid = _create_campaign(client)
    assert _upload(client, cid, "Resume.pdf", b"123456").status_code == 200
    monkeypatch.setattr(ea, "MAX_ATTACHMENTS_TOTAL_BYTES", 3)
    with pytest.raises(RuntimeError):
        ea._campaign_attachments(cid)


def test_campaign_attachments_skips_missing_files(client: TestClient, isolated: Path):
    cid = _create_campaign(client)
    assert _upload(client, cid, "Resume.pdf", b"%PDF-1.4 r").status_code == 200
    for f in (isolated / "attachments" / cid).glob("*"):
        f.unlink()
    assert ea._campaign_attachments(cid) == []


# ---------------------------------------------------------------------------
# Sender connection — SMTP
# ---------------------------------------------------------------------------


def test_sender_smtp_connect_verify_disconnect(client: TestClient, monkeypatch):
    seen = {}

    def fake_login(cfg):
        seen.update(cfg)

    monkeypatch.setattr(ea, "_smtp_login", fake_login)
    # Deterministic env fallback: no app password configured in .env.
    monkeypatch.setattr(ea.settings, "GMAIL_APP_PASSWORD", "")
    monkeypatch.setattr(ea.settings, "SMTP_PASSWORD", "")

    res = client.post(
        "/email-agent/sender/smtp", headers=HEADERS,
        json={"from_email": "someone@gmail.com", "password": "abcd efgh ijkl mnop"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["verified"] is True
    assert data["host"] == "smtp.gmail.com" and data["port"] == 587
    assert seen["user"] == "someone@gmail.com"
    assert seen["password"] == "abcdefghijklmnop"  # app-password spaces normalized

    status = client.get("/email-agent/sender").json()
    assert status["smtp"]["configured"] is True
    assert status["smtp"]["source"] == "saved"
    assert status["smtp"]["from_email"] == "someone@gmail.com"

    # Saved credentials win over the .env fallback for real sends.
    cfg = ea._smtp_config("smtp")
    assert cfg["from"] == "someone@gmail.com"
    assert cfg["password"] == "abcdefghijklmnop"

    health = client.get("/email-agent/health").json()
    assert health["smtp"] is True
    assert health["smtp_from"] == "someone@gmail.com"

    res = client.post("/email-agent/sender/smtp/disconnect", headers=HEADERS)
    assert res.status_code == 200
    status = client.get("/email-agent/sender").json()
    assert status["smtp"]["configured"] is False
    assert status["smtp"]["source"] is None


def test_sender_smtp_rejects_bad_credentials(client: TestClient, monkeypatch):
    def boom(cfg):
        raise smtplib.SMTPAuthenticationError(535, b"Bad credentials")

    monkeypatch.setattr(ea, "_smtp_login", boom)
    res = client.post(
        "/email-agent/sender/smtp", headers=HEADERS,
        json={"from_email": "a@gmail.com", "password": "wrong"},
    )
    assert res.status_code == 400
    assert "Could not sign in" in res.json()["detail"]
    status = client.get("/email-agent/sender").json()
    assert status["smtp"]["source"] != "saved"


def test_sender_smtp_non_gmail_needs_host(client: TestClient, monkeypatch):
    monkeypatch.setattr(ea, "_smtp_login", lambda cfg: None)
    res = client.post(
        "/email-agent/sender/smtp", headers=HEADERS,
        json={"from_email": "bob@company.com", "password": "secret"},
    )
    assert res.status_code == 400
    assert "SMTP host" in res.json()["detail"]

    ok = client.post(
        "/email-agent/sender/smtp", headers=HEADERS,
        json={"from_email": "bob@company.com", "password": "secret",
              "host": "mail.company.com", "port": 465},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["ssl"] is True  # port 465 implies implicit TLS
    status = client.get("/email-agent/sender").json()
    assert status["smtp"]["host"] == "mail.company.com"
    assert status["smtp"]["ssl"] is True


@pytest.mark.skipif(not API_KEY, reason="SARA_API_KEY not configured")
def test_sender_connect_requires_api_key(client: TestClient):
    res = client.post(
        "/email-agent/sender/smtp",
        json={"from_email": "a@gmail.com", "password": "x"},
    )
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Sender connection — Google status surface
# ---------------------------------------------------------------------------


def test_sender_gmail_status_surface(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        ea, "_gmail_status", lambda: {"connected": True, "email": "me@gmail.com"}
    )
    status = client.get("/email-agent/sender").json()
    assert status["gmail"]["connected"] is True
    assert status["gmail"]["email"] == "me@gmail.com"
    assert status["app_passwords_url"].startswith("https://")
