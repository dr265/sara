"""_jarvis_dashboard_certs.py — self-signed TLS cert for the phone Remote Dashboard.

The phone UI is served by sara_desktop/dashboard/server.py. Plain HTTP works for
text chat, but mobile browsers only expose the microphone (getUserMedia) in a
"secure context" — so the dashboard serves HTTPS when these cert files exist:

    sara_desktop/config/certs/sara.key
    sara_desktop/config/certs/sara.crt

The cert covers localhost, the machine hostname and every current local IPv4
(SAN entries), so whatever address the phone uses it matches. Self-signed, so
the phone shows a one-time "not private" warning — accept it once.

Re-runnable: regenerates and overwrites. The desktop app picks the certs up on
next start (server._ssl_enabled()). Keeps a 10-year validity.

Run:  sara_desktop\\venv\\Scripts\\python.exe _jarvis_dashboard_certs.py
"""

import datetime
import ipaddress
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "sara_desktop" / "config" / "certs"

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def local_ips() -> list[str]:
    ips: set[str] = set()
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(ip for ip in ips if not ip.startswith("169.254."))


def main() -> int:
    host = socket.gethostname() or "sara"
    ips = [ipaddress.ip_address("127.0.0.1")]
    for raw in local_ips():
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if str(ip) != "127.0.0.1":
            ips.append(ip)

    sans: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName(host),
    ] + [x509.IPAddress(ip) for ip in ips]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                          "SARA Local Dashboard")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sara.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    (OUT / "sara.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    print(f"OK  {OUT / 'sara.key'}")
    print(f"OK  {OUT / 'sara.crt'}")
    print("SANs: localhost, " + host + ", " + ", ".join(str(i) for i in ips))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
