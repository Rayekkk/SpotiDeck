"""Short-lived, device-local HTTPS for phone OAuth. No cloud relay or tokens in pages."""
import hashlib
import ipaddress
import os
import socket
import ssl
import subprocess
import tempfile
from pathlib import Path


PHONE_PORT = 43892


def lan_address():
    """Ask the routing table for the LAN interface without sending network traffic."""
    candidates = []
    for destination in ("192.0.2.1", "198.51.100.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((destination, 9))
                candidates.append(probe.getsockname()[0])
        except OSError:
            pass
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    private_ranges = tuple(ipaddress.ip_network(value) for value in
                           ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    for value in candidates:
        address = ipaddress.ip_address(value)
        if any(address in network for network in private_ranges):
            return str(address)
    raise OSError("No private Wi-Fi or Ethernet address is available.")


def phone_tls(directory, address):
    """Create our own certificate; outgoing Spotify HTTPS verification is unchanged."""
    address = str(ipaddress.IPv4Address(address))
    directory = Path(directory) / "phone-login"
    if directory.is_symlink():
        raise OSError("Unsafe phone sign-in directory.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    cert, key = directory / "certificate.pem", directory / "private-key.pem"
    address_file = directory / "address"
    if any(path.is_symlink() for path in (cert, key, address_file)):
        raise OSError("Unsafe phone sign-in files.")
    needs_certificate = (not cert.is_file() or not key.is_file() or not address_file.is_file()
                         or address_file.read_text(encoding="ascii") != address)
    if not needs_certificate:
        try:
            environment = _openssl_environment()
            result = subprocess.run(["openssl", "x509", "-checkend", "86400", "-noout", "-in", str(cert)],
                                    capture_output=True, timeout=10, env=environment)
            needs_certificate = result.returncode != 0
        except OSError:
            needs_certificate = True
    if needs_certificate:
        with tempfile.TemporaryDirectory(prefix=".certificate-", dir=directory) as temporary:
            temporary = Path(temporary)
            result = subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
                "-days", "365", "-keyout", str(temporary / "key.pem"),
                "-out", str(temporary / "cert.pem"), "-subj", "/CN=SpotiDeck phone login",
                "-addext", "subjectAltName=IP:" + address,
            ], capture_output=True, timeout=20, env=_openssl_environment())
            if result.returncode:
                raise OSError("Cannot create the device's phone sign-in certificate.")
            for source, target in ((temporary / "key.pem", key), (temporary / "cert.pem", cert)):
                os.chmod(source, 0o600)
                os.replace(source, target)
            address_file.write_text(address, encoding="ascii")
            os.chmod(address_file, 0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="ascii"))
    digest = hashlib.sha256(der).hexdigest().upper()
    fingerprint = ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))
    return context, fingerprint


def _openssl_environment():
    environment = dict(os.environ)
    for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH"):
        environment.pop(name, None)
    # An appliance's frozen Python runtime may point OpenSSL at bundled modules.
    for name in ("OPENSSL_CONF", "OPENSSL_MODULES"):
        environment.pop(name, None)
    return environment
