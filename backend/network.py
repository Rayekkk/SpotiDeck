"""Verified HTTPS using the host trust store, including frozen Decky runtimes."""
from functools import lru_cache
from pathlib import Path
import ssl
import sys
import urllib.error
import urllib.request


SYSTEM_CA_FILES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
)


@lru_cache(maxsize=1)
def tls_context():
    context = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    # Decky's bundled OpenSSL can reference its build machine's /usr/lib/ssl,
    # which does not exist on SteamOS. Keep valid configured defaults intact.
    if sys.platform.startswith("linux") and not paths.cafile and not paths.capath:
        for candidate in SYSTEM_CA_FILES:
            if Path(candidate).is_file():
                context.load_verify_locations(cafile=candidate)
                break
    return context


def https_opener(*handlers):
    return urllib.request.build_opener(*handlers, urllib.request.HTTPSHandler(context=tls_context()))


def is_certificate_error(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    return isinstance(reason, ssl.SSLCertVerificationError)
