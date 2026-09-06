import asyncio
import base64
import hashlib
import html
import json
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .network import https_opener, is_certificate_error
from .phone_auth import PHONE_PORT, lan_address, phone_tls

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REFRESH_FAILURE_COOLDOWN = 2
REDIRECT = "http://127.0.0.1:43891/callback"
SCOPES = " ".join(("user-read-private", "user-read-playback-state",
                   "user-modify-playback-state", "user-read-currently-playing",
                   "playlist-read-private", "playlist-read-collaborative",
                   "user-library-read", "user-library-modify", "user-follow-read", "user-follow-modify",
                   "user-read-recently-played", "user-top-read", "user-read-playback-position",
                   "playlist-modify-public", "playlist-modify-private"))


class SpotifyError(Exception):
    def __init__(self, message, code="spotify_error", retry_after=0):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(method, url, headers=None, payload=None):
    """Return errors as data; do not expose response bodies, tokens or URLs in logs."""
    req = urllib.request.Request(url, data=payload, headers=headers or {}, method=method)
    try:
        with https_opener(NoRedirect).open(req, timeout=12) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise SpotifyError("Spotify returned too much data.")
            # Web API writes in this plugin are commands; their HTTP status is
            # the acknowledgement. Spotify also returns 200 with non-JSON text.
            # OAuth token exchanges and data reads still require valid JSON.
            path = urllib.parse.urlsplit(url).path
            json_write = path == "/v1/me/playlists" or re.fullmatch(r"/v1/playlists/[A-Za-z0-9]{22}/items", path)
            if url.startswith(API + "/") and method in ("PUT", "POST", "DELETE") and not json_write:
                return response.status, dict(response.headers), None
            try:
                data = json.loads(raw) if raw.strip() else None
            except (ValueError, UnicodeError):
                raise SpotifyError("Spotify returned an invalid response. Please try again.") from None
            return response.status, dict(response.headers), data
    except urllib.error.HTTPError as error:
        # Error bodies can echo OAuth values, so only retain the machine-readable code.
        raw = error.read(8192)
        try:
            detail = json.loads(raw)
        except (ValueError, UnicodeError):
            detail = {}
        return error.code, dict(error.headers), detail
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        if is_certificate_error(error):
            raise SpotifyError("Spotify's secure connection could not be verified. Check your handheld's date and system certificates.", "tls") from None
        raise SpotifyError("Cannot reach Spotify. Check your internet connection.", "offline") from None


class Spotify:
    def __init__(self, store, transport=http):
        self.store = store
        self.transport = transport
        self.token_lock = asyncio.Lock()
        self.auth_lock = asyncio.Lock()
        self.epoch = 0
        self.pending = None
        self.server = None
        self.expiry_task = None
        self.auth_error = None
        self.backoff_until = 0
        self.refresh_failure = None
        self.server_closures = set()
        self.callback_tasks = set()
        self.callback_writers = set()

    @property
    def connected(self):
        return bool(self.store.data.get("refresh_token"))

    @property
    def needs_reauthorization(self):
        granted = self.store.data.get("granted_scopes") or ""
        if not isinstance(granted, str):
            granted = ""
        return self.connected and not set(SCOPES.split()).issubset(set(granted.split()))

    async def begin_auth(self, client_id, mode="handheld"):
        if not isinstance(client_id, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", client_id.strip()):
            raise SpotifyError("Enter the Client ID from your Spotify developer app.", "setup")
        if mode not in ("handheld", "phone"):
            raise SpotifyError("Choose sign-in on this handheld or by phone.", "setup")
        async with self.auth_lock:
            await self.cancel_auth()
            self.epoch += 1
            self.refresh_failure = None
            verifier = secrets.token_urlsafe(64)
            state = secrets.token_urlsafe(32)
            bind, port, redirect, context, fingerprint = "127.0.0.1", 43891, REDIRECT, None, None
            try:
                if mode == "phone":
                    bind = await asyncio.to_thread(lan_address)
                    port = PHONE_PORT
                    redirect = f"https://{bind}:{port}/callback"
                    context, fingerprint = await asyncio.to_thread(phone_tls, self.store.directory, bind)
                self.pending = {"state": state, "verifier": verifier, "client_id": client_id.strip(),
                                "expires": time.monotonic() + 300, "epoch": self.epoch,
                                "redirect": redirect, "mode": mode, "phone_path": "/phone/" + secrets.token_urlsafe(32)}
                options = {"ssl": context, "ssl_handshake_timeout": 5} if context else {}
                self.server = await asyncio.start_server(self._callback, bind, port, limit=16384, **options)
            except (OSError, TimeoutError, ValueError, subprocess.SubprocessError):
                self.pending = None
                message = ("Phone sign-in could not start. Connect the handheld to Wi-Fi, check that OpenSSL is installed, "
                           "or use sign-in on this handheld." if mode == "phone" else
                           "The sign-in port is busy. Close the other sign-in session and retry.")
                raise SpotifyError(message, "setup") from None
            self.expiry_task = asyncio.create_task(self._expire())
            self.auth_error = None
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
            params = {"response_type": "code", "client_id": client_id.strip(), "redirect_uri": redirect,
                      "scope": SCOPES, "state": state, "code_challenge_method": "S256", "code_challenge": challenge}
            authorization = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)
            self.pending["authorization"] = authorization
            url = redirect.removesuffix("/callback") + self.pending["phone_path"] if mode == "phone" else authorization
            return {"url": url, "redirect": redirect, "mode": mode, "expiresAt": int(time.time() * 1000) + 300000,
                    "certificateFingerprint": fingerprint}

    async def _expire(self):
        await asyncio.sleep(300)
        self.pending = None
        self.auth_error = "Sign-in timed out. Select Connect to Spotify to try again."
        await self._close_server()

    async def _close_server(self):
        server, self.server = self.server, None
        if server:
            server.close()
            # Python 3.12+ waits for accepted connections as well as the listener.
            # The callback must send its response and close before this can finish.
            task = asyncio.create_task(server.wait_closed())
            self.server_closures.add(task)
            task.add_done_callback(self.server_closures.discard)

    async def cancel_auth(self):
        self.pending = None
        if self.expiry_task and self.expiry_task is not asyncio.current_task():
            self.expiry_task.cancel()
            await asyncio.gather(self.expiry_task, return_exceptions=True)
        self.expiry_task = None
        await self._close_server()

    async def close(self):
        """Close owned callback connections and drain their server during unload."""
        self.epoch += 1
        self.refresh_failure = None
        await self.cancel_auth()
        for writer in tuple(self.callback_writers):
            writer.close()
        tasks = tuple(task for task in self.callback_tasks if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*tuple(self.server_closures), return_exceptions=True)

    async def _callback(self, reader, writer):
        task = asyncio.current_task()
        self.callback_tasks.add(task)
        self.callback_writers.add(writer)
        status, message = "400 Bad Request", "Unable to complete Spotify sign-in. Return to Decky and retry."
        extra = ""
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            line = raw.split(b"\r\n", 1)[0].decode("ascii")
            method, target, protocol = line.split(" ", 2)
            pending = self.pending
            headers = [line.decode("ascii").split(":", 1) for line in raw.split(b"\r\n")[1:] if b":" in line]
            hosts = [value.strip() for name, value in headers if name.lower() == "host"]
            expected = urllib.parse.urlsplit(pending["redirect"]).netloc if pending else None
            valid = (method == "GET" and protocol in ("HTTP/1.0", "HTTP/1.1") and hosts == [expected]
                     and pending and time.monotonic() <= pending["expires"])
            if valid and pending["mode"] == "phone" and secrets.compare_digest(target, pending["phone_path"]):
                status, message = "200 OK", "Continue to Spotify to connect this handheld. This page closes after five minutes."
                extra = '<p><a href="' + html.escape(pending["authorization"], quote=True) + '">Continue to Spotify</a></p>'
            elif valid and target.startswith("/callback?"):
                await self.finish_auth(pending["redirect"].removesuffix("/callback") + target)
                status, message = "200 OK", "Connected to Spotify. You can close this page and return to Decky."
        except SpotifyError as error:
            message = str(error)
            self.auth_error = message
        except (ValueError, UnicodeError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            body = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>SpotiDeck</title><style>body{font:18px system-ui;background:#121212;color:white;padding:24px;max-width:600px;margin:auto}"
                    "a{display:inline-block;background:#1db954;color:#000;padding:18px 24px;border-radius:28px;text-decoration:none;font-weight:bold}</style>"
                    "<h1>SpotiDeck</h1><p>" + html.escape(message) + "</p>" + extra).encode("utf-8")
            try:
                if not writer.is_closing():
                    writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                                  "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nX-Content-Type-Options: nosniff\r\n"
                                  "Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'\r\n"
                                  f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("ascii") + body)
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
                finally:
                    self.callback_tasks.discard(task)
                    self.callback_writers.discard(writer)

    async def finish_auth(self, url):
        if not isinstance(url, str) or len(url) > 8192:
            raise SpotifyError("Invalid sign-in callback.", "auth")
        parsed = urllib.parse.urlsplit(url)
        params = urllib.parse.parse_qs(parsed.query)
        async with self.auth_lock:
            pending = self.pending
            expected = urllib.parse.urlsplit(pending.get("redirect", REDIRECT) if pending else REDIRECT)
            if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path) or parsed.fragment:
                raise SpotifyError("Invalid sign-in callback.", "auth")
            if (not pending or time.monotonic() > pending["expires"]
                    or not secrets.compare_digest(params.get("state", [""])[0].encode("utf-8"), pending["state"].encode("ascii"))):
                raise SpotifyError("This sign-in request is invalid or has expired.", "auth")
            if any(len(values) != 1 for values in params.values()):
                raise SpotifyError("Invalid sign-in callback.", "auth")
            self.pending = None  # One use, including failed or denied authorizations.
            await self.cancel_auth()
            if params.get("error") or not params.get("code"):
                self.auth_error = "Spotify sign-in was cancelled."
                raise SpotifyError(self.auth_error, "auth")
            body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": params["code"][0],
                                          "redirect_uri": pending.get("redirect", REDIRECT), "client_id": pending["client_id"],
                                          "code_verifier": pending["verifier"]}).encode("ascii")
            status, _, data = await asyncio.to_thread(self.transport, "POST", TOKEN_URL,
                                                       {"Content-Type": "application/x-www-form-urlencoded"}, body)
            if (status != 200 or not isinstance(data, dict) or not isinstance(data.get("refresh_token"), str)
                    or not data.get("refresh_token") or not isinstance(data.get("access_token"), str) or not data.get("access_token")):
                self.auth_error = "Spotify could not complete sign-in. Check the Client ID and redirect URI."
                raise SpotifyError(self.auth_error, "auth")
            if self.epoch != pending["epoch"]:
                raise SpotifyError("This sign-in session was cancelled.", "auth")
            self.store.update(client_id=pending["client_id"], access_token=data["access_token"],
                              refresh_token=data["refresh_token"], expires_at=time.time() + int(data.get("expires_in", 3600)),
                              granted_scopes=data.get("scope", SCOPES))
            # Reads made with the previous account while OAuth was open must not
            # populate the newly authorized account's profile/library caches.
            self.epoch += 1
            self.auth_error = None
            self.backoff_until = 0
            self.refresh_failure = None

    async def disconnect(self):
        self.epoch += 1
        self.refresh_failure = None
        await self.cancel_auth()
        async with self.token_lock:
            self.store.update(access_token=None, refresh_token=None, expires_at=0, granted_scopes=None)
        self.auth_error = None
        self.backoff_until = 0

    async def token(self):
        async with self.token_lock:
            data = self.store.data
            identity = (self.epoch, data.get("client_id"), data.get("refresh_token"))
            if self.refresh_failure and self.refresh_failure["identity"] != identity:
                self.refresh_failure = None
            if not data.get("refresh_token"):
                raise SpotifyError("Connect your Spotify account to continue.", "auth")
            # A preceding refresh can establish Retry-After while this caller
            # waits for the lock, after request() already checked the limit.
            self._check_backoff()
            if data.get("access_token") and data.get("expires_at", 0) > time.time() + 45:
                return data["access_token"]
            if self.refresh_failure:
                failure = self.refresh_failure
                if time.monotonic() < failure["until"]:
                    raise SpotifyError(failure["message"], failure["code"])
                self.refresh_failure = None
            epoch = self.epoch
            body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                                          "client_id": data["client_id"]}).encode("ascii")
            try:
                status, headers, result = await asyncio.to_thread(self.transport, "POST", TOKEN_URL,
                                                                  {"Content-Type": "application/x-www-form-urlencoded"}, body)
                if epoch != self.epoch:
                    raise SpotifyError("Your account changed. Please try again.", "auth")
                if status == 400 or status == 401:
                    self.store.update(access_token=None, refresh_token=None, expires_at=0)
                    raise SpotifyError("Your Spotify session expired. Please connect again.", "auth")
                self._check(status, headers)
                if not isinstance(result, dict) or not result.get("access_token"):
                    raise SpotifyError("Spotify returned an invalid sign-in response.", "auth")
                self.store.update(access_token=result["access_token"], refresh_token=result.get("refresh_token", data["refresh_token"]),
                                  expires_at=time.time() + int(result.get("expires_in", 3600)))
            except SpotifyError as error:
                current = self.store.data
                if epoch != self.epoch:
                    raise SpotifyError("Your account changed. Please try again.", "auth") from None
                if (current.get("refresh_token") and error.code != "rate_limit"
                        and identity == (self.epoch, current.get("client_id"), current.get("refresh_token"))):
                    # Keep only public error data, not its traceback or response.
                    # Waiting profile/playback reads share one failed attempt;
                    # future reads can retry after this brief recovery window.
                    self.refresh_failure = {"identity": identity,
                                            "until": time.monotonic() + REFRESH_FAILURE_COOLDOWN,
                                            "message": str(error), "code": error.code}
                raise
            self.refresh_failure = None
            return result["access_token"]

    def _check_backoff(self):
        remaining = self.backoff_until - time.monotonic()
        if remaining > 0:
            raise SpotifyError("Spotify is limiting requests. Please wait before retrying.", "rate_limit", int(remaining) + 1)

    def _check(self, status, headers):
        if status == 429:
            try:
                delay = max(1, min(86400, int(next((v for k, v in headers.items() if k.lower() == "retry-after"), 60))))
            except (ValueError, TypeError):
                delay = 60
            self.backoff_until = time.monotonic() + delay
            raise SpotifyError(f"Spotify's request limit was reached. Try again in {delay} seconds.", "rate_limit", delay)
        if status == 403:
            if self.needs_reauthorization:
                raise SpotifyError("Reconnect Spotify in Settings to grant access to the new library features.", "restricted")
            raise SpotifyError("Spotify denied access. Check Premium, app access and whether this playlist is yours or collaborative.", "restricted")
        if status == 404:
            raise SpotifyError("Spotify could not find this item or an active playback device. Check Devices.", "not_found")
        if status == 401:
            raise SpotifyError("Please reconnect your Spotify account.", "auth")
        if not 200 <= status < 300:
            raise SpotifyError("Spotify could not complete this request. Please try again.", "spotify_error")

    async def request(self, method, path, params=None, body=None):
        if not path.startswith("/") or "?" in path or ".." in path:
            raise ValueError("Invalid API path")
        self._check_backoff()
        epoch = self.epoch
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(2):
            token = await self.token()
            status, headers, result = await asyncio.to_thread(self.transport, method, url,
                        {"Authorization": "Bearer " + token, "Content-Type": "application/json"}, payload)
            if epoch != self.epoch:
                raise SpotifyError("Your account changed. Please try again.", "auth")
            if status == 401 and attempt == 0:
                self.store.update(expires_at=0)
                continue
            self._check(status, headers)
            return result
