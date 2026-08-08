"""
redfish/session.py
===================
Implements DMTF Redfish Session-based authentication
(POST /redfish/v1/SessionService/Sessions).

Responsibilities
----------------
- Create a session against a BMC and capture the X-Auth-Token + the
  session's own @odata.id (Location header) so it can be explicitly
  deleted (logged out) later.
- Persist sessions in the database (RedfishSessionRecord) so multiple
  poller workers can share a session for the same server instead of each
  opening a fresh one (BMCs enforce a low max-concurrent-session limit,
  often 4-6).
- Detect an expired/invalidated session (HTTP 401 on a resource request)
  and transparently re-authenticate.
- Fall back to HTTP Basic Auth only if SessionService is unavailable
  (some minimal BMC implementations only support Basic).

This module intentionally knows nothing about *what* resources exist on a
BMC - that is discovery.py's job. It only knows how to get and keep a
valid, authenticated HTTP client.

HPE iLO / Legacy TLS Notes
---------------------------
iLO 2 and iLO 3 have NO Redfish API (RIBCL/XML only) and cannot be
supported by this application regardless of TLS settings.

iLO 4 firmware < 2.30 has no Redfish. iLO 4 firmware >= 2.30 supports
Redfish 1.0 but may use TLS 1.0/1.1 with legacy ciphers depending on
the firmware version and iLO security settings.

The legacy TLS fallback in _build_ssl_context() enables TLS 1.0/1.1 and
sets OpenSSL SECLEVEL=0 (all ciphers permitted) to handle these cases.
This is consistent with REDFISH_VERIFY_TLS=false which the project uses
for lab BMC environments with self-signed certificates.

SSL error detection: OpenSSL handshake failures produce error strings
like WRONG_VERSION_NUMBER, UNSUPPORTED_PROTOCOL, NO_SHARED_CIPHER,
HANDSHAKE_FAILURE, ALERT_HANDSHAKE_FAILURE — NOT the literal word
"SSL". The _is_ssl_error() helper catches all of these so the legacy
TLS retry fires correctly on the first attempt.
"""
import logging
import threading
from datetime import datetime, timedelta

import httpx
import ssl


# OpenSSL error code substrings that indicate a TLS/SSL protocol-level
# failure rather than a plain TCP/network failure.  These appear in the
# string representation of httpx.ConnectError when the underlying SSL
# handshake fails.  The literal word "SSL" is NOT always present — many
# errors only contain the OpenSSL symbolic name (e.g. WRONG_VERSION_NUMBER).
_SSL_ERROR_SUBSTRINGS = (
    "ssl",                       # generic ssl prefix
    "certificate",               # cert validation error
    "handshake",                 # handshake_failure, alert_handshake_failure
    "wrong_version_number",      # server uses TLS version client won't accept
    "unsupported_protocol",      # server protocol completely unsupported
    "no_shared_cipher",          # cipher suite mismatch
    "no_protocols_available",    # all protocols disabled on one side
    "sslv3_alert",               # SSLv3 alert class
    "tlsv1_alert",               # TLS 1.x alert class
    "unknown_protocol",          # server replied with non-TLS data
    "record layer failure",      # broken framing
    "connection reset",          # some iLO devices RST on TLS mismatch
)


def _is_ssl_error(exc: Exception) -> bool:
    """Return True when *exc* is an httpx.ConnectError whose cause is a
    TLS/SSL protocol failure (as opposed to a plain TCP refusal or DNS
    error).  Checks against the full set of known OpenSSL error substrings
    rather than just 'SSL' or 'certificate', which are absent from many
    real handshake failure messages."""
    if not isinstance(exc, httpx.ConnectError):
        return False
    msg = str(exc).lower()
    return any(s in msg for s in _SSL_ERROR_SUBSTRINGS)


def _build_ssl_context(verify: bool, legacy_fallback: bool) -> ssl.SSLContext:
    """Build an ssl.SSLContext appropriate for BMC connections.

    verify=False  : disables certificate and hostname verification, which
                    is required for BMCs with self-signed certificates
                    (controlled by REDFISH_VERIFY_TLS in the config).
    legacy_fallback=True : enables TLS 1.0/1.1 and lowers OpenSSL SECLEVEL
                    to 0, allowing legacy cipher suites used by older BMC
                    firmware (e.g. HPE iLO 4 early firmware, iDRAC 8 early
                    firmware).  This is tried automatically on the second
                    connection attempt when the first fails with any TLS
                    handshake error.
    """
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    if legacy_fallback:
        # Remove TLS version floor restrictions so TLS 1.0/1.1 are allowed.
        ctx.options &= ~ssl.OP_NO_TLSv1
        ctx.options &= ~ssl.OP_NO_TLSv1_1
        # Also try to remove SSLv3 block where supported by the platform.
        # Some very old iLO firmware uses SSLv3; OpenSSL 3.x removes this
        # constant so we guard with getattr.
        _OP_NO_SSLv3 = getattr(ssl, "OP_NO_SSLv3", None)
        if _OP_NO_SSLv3 is not None:
            ctx.options &= ~_OP_NO_SSLv3

        if hasattr(ctx, "minimum_version"):
            try:
                ctx.minimum_version = ssl.TLSVersion.TLSv1
            except (AttributeError, ssl.SSLError):
                pass  # TLSv1 not available on this OpenSSL build

        # SECLEVEL=0 removes all cipher strength restrictions, which is
        # required for some iLO 4 firmware that still uses DHE-512 or RC4.
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            try:
                ctx.set_ciphers("ALL:@SECLEVEL=0")
            except ssl.SSLError:
                ctx.set_ciphers("ALL")  # last resort

    return ctx

logger = logging.getLogger(__name__)

SESSION_SERVICE_PATH = "/redfish/v1/SessionService/Sessions/"
DEFAULT_ASSUMED_SESSION_TTL_MINUTES = 30  # most BMCs default to 30 min idle timeout


class RedfishAuthError(Exception):
    """Raised when credentials are rejected (HTTP 401/403 on session create)."""


class RedfishUnreachableError(Exception):
    """Raised when the BMC cannot be reached at all (network/TLS/timeout)."""


def format_httpx_exception(exc: Exception) -> str:
    """Return a human-readable, actionable description of an httpx exception.

    Distinguishes between network-level failures (TCP, DNS), TLS-level
    failures (handshake, version mismatch, cipher mismatch, certificate),
    and timeout failures so operators can diagnose the actual problem
    without reading raw OpenSSL error codes.
    """
    msg = str(exc)
    msg_lower = msg.lower()

    if isinstance(exc, httpx.ConnectTimeout):
        return f"TCP connect timed out — BMC unreachable or firewall blocking port 443: {msg}"
    if isinstance(exc, httpx.ReadTimeout):
        return f"Read timed out — BMC accepted the connection but did not respond in time: {msg}"
    if isinstance(exc, httpx.WriteTimeout):
        return f"Write timed out — BMC accepted the connection but could not receive the request: {msg}"
    if isinstance(exc, httpx.TimeoutException):
        return f"Timeout: {msg}"

    if isinstance(exc, httpx.ConnectError):
        # Classify the TLS/SSL sub-type for actionable diagnostics.
        if "wrong_version_number" in msg_lower or "unsupported_protocol" in msg_lower:
            return (
                f"TLS version mismatch — the BMC may require TLS 1.0/1.1 (legacy firmware) "
                f"or the device has no Redfish API (e.g. HPE iLO 2/3): {msg}"
            )
        if "no_shared_cipher" in msg_lower or "no_protocols_available" in msg_lower:
            return (
                f"TLS cipher mismatch — no common cipher suite with BMC "
                f"(legacy device may need SECLEVEL=0): {msg}"
            )
        if "handshake" in msg_lower or "sslv3_alert" in msg_lower or "tlsv1_alert" in msg_lower:
            return (
                f"TLS handshake failed — BMC rejected the TLS negotiation "
                f"(possible legacy firmware or unsupported TLS version): {msg}"
            )
        if "certificate" in msg_lower:
            return f"TLS certificate error — BMC certificate is invalid or untrusted: {msg}"
        if "ssl" in msg_lower or "unknown_protocol" in msg_lower or "record layer" in msg_lower:
            return f"TLS/SSL error: {msg}"
        if "connection refused" in msg_lower:
            return f"TCP connection refused — BMC port 443 is closed or filtered: {msg}"
        if "name or service not known" in msg_lower or "getaddrinfo" in msg_lower:
            return f"DNS resolution failed — check the IP address or hostname: {msg}"
        return f"Connection error — BMC unreachable: {msg}"

    if isinstance(exc, httpx.NetworkError):
        return f"Network error: {msg}"
    return str(exc)


class RedfishSession:
    """
    Holds live authentication state for a single BMC. One instance per
    server, cached by SessionManager. Thread-safe: multiple poller threads
    for different servers each get their own instance, but a given
    instance's token refresh is locked so we don't double-authenticate.
    """

    def __init__(self, base_url: str, username: str, password: str, config, server_id: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.config = config
        self.server_id = server_id

        self.token: str | None = None
        self.session_uri: str | None = None
        self.expires_at: datetime | None = None
        self.uses_basic_auth_fallback = False
        self.uses_legacy_tls = False

        self._lock = threading.Lock()
        self._client: httpx.Client | None = None

    # -- public API -------------------------------------------------------

    def get_http_client(self) -> httpx.Client:
        """Return an httpx.Client pre-configured with valid auth headers,
        authenticating or refreshing first if necessary."""
        self._ensure_valid_session()
        
        with self._lock:
            if self._client is not None:
                return self._client
                
            headers = {"OData-Version": "4.0"}
            ssl_context = _build_ssl_context(self.config.REDFISH_VERIFY_TLS, self.uses_legacy_tls)
            
            if self.uses_basic_auth_fallback:
                auth = (self.username, self.password)
                self._client = httpx.Client(
                    base_url=self.base_url,
                    headers=headers,
                    auth=auth,
                    verify=ssl_context,
                    timeout=self.config.REDFISH_HTTP_TIMEOUT,
                    follow_redirects=True,
                )
            else:
                headers["X-Auth-Token"] = self.token
                self._client = httpx.Client(
                    base_url=self.base_url,
                    headers=headers,
                    verify=ssl_context,
                    timeout=self.config.REDFISH_HTTP_TIMEOUT,
                    follow_redirects=True,
                )
            return self._client

    def invalidate(self):
        """Force the next call to re-authenticate (e.g. after a 401)."""
        with self._lock:
            self.token = None
            self.expires_at = None
            self.uses_basic_auth_fallback = False
            if self._client is not None:
                self._client.close()
                self._client = None

    def logout(self):
        """Explicitly DELETE the session on the BMC to free the session slot."""
        if self.uses_basic_auth_fallback or not self.token or not self.session_uri:
            return
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers={"X-Auth-Token": self.token},
                verify=_build_ssl_context(self.config.REDFISH_VERIFY_TLS, self.uses_legacy_tls),
                timeout=self.config.REDFISH_HTTP_TIMEOUT,
                follow_redirects=True,
            ) as client:
                client.delete(self.session_uri)
        except httpx.HTTPError as exc:
            logger.warning("Logout failed for %s: %s", self.base_url, exc)
        finally:
            self.token = None
            self.session_uri = None
            self.expires_at = None
            with self._lock:
                if self._client is not None:
                    self._client.close()
                    self._client = None

    # -- internals ----------------------------------------------------------

    def _ensure_valid_session(self):
        with self._lock:
            if self.uses_basic_auth_fallback:
                return
            if self.token and self.expires_at and datetime.utcnow() < (
                self.expires_at - self.config.REDFISH_SESSION_REFRESH_MARGIN
            ):
                return
            self._authenticate()

    def _authenticate(self):
        """POST credentials to SessionService/Sessions and capture the token.

        Lenovo XClarity Controller (XCC) compatibility note
        ----------------------------------------------------
        XCC uses /redfish/v1/SessionService/Sessions/Members as the POST
        target for new sessions instead of the standard /Sessions path.
        Additionally, GET /redfish/v1/SessionService/ itself requires
        authentication on XCC (returns 401), so dynamic discovery of the
        Sessions URL fails silently.  We therefore attempt the standard
        /Sessions path first, and if it returns 401 we retry with the
        /Members sub-path before concluding the credentials are wrong.
        """
        payload = {"UserName": self.username, "Password": self.password}
        sessions_url = SESSION_SERVICE_PATH
        for attempt in range(2):
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    verify=_build_ssl_context(self.config.REDFISH_VERIFY_TLS, self.uses_legacy_tls),
                    timeout=self.config.REDFISH_HTTP_TIMEOUT,
                    follow_redirects=True,
                ) as client:
                    # Attempt to dynamically discover the Sessions URL from the ServiceRoot
                    try:
                        root_resp = client.get("/redfish/v1/")
                        if root_resp.status_code == 200:
                            root_data = root_resp.json()
                            session_service_url = root_data.get("SessionService", {}).get("@odata.id")
                            if session_service_url:
                                svc_resp = client.get(session_service_url)
                                if svc_resp.status_code == 200:
                                    svc_data = svc_resp.json()
                                    sessions_url = svc_data.get("Sessions", {}).get("@odata.id", sessions_url)
                                    # Also check for a Members navigation link (Lenovo XCC pattern)
                                    members_nav = svc_data.get("Sessions", {}).get("Members@odata.navigationLink")
                                    if members_nav:
                                        sessions_url = members_nav
                    except Exception as e:
                        logger.debug("Failed to dynamically discover SessionService URL: %s", e)

                    resp = client.post(sessions_url, json=payload)

                    # ── Lenovo XClarity Controller (XCC) compatibility ──────────────────
                    # XCC's SessionService endpoint is itself protected: GET
                    # /redfish/v1/SessionService/ returns 401 so dynamic discovery
                    # above cannot resolve the Sessions URL.  XCC requires the POST
                    # to go to /redfish/v1/SessionService/Sessions/Members, NOT the
                    # DMTF-standard /Sessions path.  When the standard path returns
                    # 401, we retry with the /Members sub-path before declaring the
                    # credentials invalid.  This is safe because a genuine wrong-
                    # credentials 401 will also come back from /Members.
                    if resp.status_code == 401 and not sessions_url.rstrip("/").endswith("/Members"):
                        members_url = sessions_url.rstrip("/") + "/Members"
                        logger.info(
                            "POST %s returned 401 on %s — retrying with Lenovo XCC "
                            "/Members sub-path: %s",
                            sessions_url, self.base_url, members_url,
                        )
                        resp_members = client.post(members_url, json=payload)
                        if resp_members.status_code in (200, 201):
                            resp = resp_members
                            logger.info(
                                "Lenovo XCC /Members endpoint succeeded (HTTP %s) on %s",
                                resp.status_code, self.base_url,
                            )
                        else:
                            # /Members also failed — keep the original resp so the
                            # error handling below sees the correct status code.
                            logger.debug(
                                "Lenovo XCC /Members fallback returned HTTP %s on %s — "
                                "credentials are likely wrong",
                                resp_members.status_code, self.base_url,
                            )
                break
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                formatted = format_httpx_exception(exc)
                # Retry with legacy TLS (TLS 1.0/1.1 + SECLEVEL=0) on the first
                # attempt when the error is a TLS handshake failure.  This covers
                # the full range of OpenSSL error codes (WRONG_VERSION_NUMBER,
                # NO_SHARED_CIPHER, HANDSHAKE_FAILURE, etc.), not just the literal
                # words "SSL" or "certificate" which are absent from many real
                # TLS errors returned by legacy BMC firmware.
                if attempt == 0 and not self.uses_legacy_tls and _is_ssl_error(exc):
                    logger.warning(
                        "TLS handshake failed on %s (%s). "
                        "Retrying with legacy TLS (TLS 1.0/1.1, SECLEVEL=0).",
                        self.base_url, formatted,
                    )
                    self.uses_legacy_tls = True
                    continue
                raise RedfishUnreachableError(f"Cannot reach {self.base_url}: {formatted}") from exc

        # Codes that mean "SessionService is not usable on this firmware" —
        # fall back to HTTP Basic Auth, which all iDRAC/iLO generations support.
        #
        #   400 Bad Request       — iDRAC 8 (firmware 2.x): returns 400 when the
        #                           SessionService POST body/format is not recognised.
        #                           THIS IS THE PRIMARY iDRAC 8 FAILURE MODE.
        #   404 Not Found         — SessionService endpoint doesn't exist at all.
        #   405 Method Not Allowed— POST is rejected by the SessionService on this BMC.
        #   501 Not Implemented   — Endpoint exists but POST is not implemented.
        _SESSION_FALLBACK_CODES = (400, 404, 405, 501)
        if resp.status_code in _SESSION_FALLBACK_CODES:
            logger.info(
                "SessionService returned HTTP %s on %s — "
                "token sessions not supported on this firmware, falling back to Basic Auth",
                resp.status_code, self.base_url,
            )
            self.uses_basic_auth_fallback = True
            self._verify_basic_auth()
            return

        if resp.status_code in (401, 403):
            logger.info(
                "Session POST returned HTTP %s on %s — "
                "verifying if this is a Lenovo XCC-style broken SessionService or genuine bad credentials",
                resp.status_code, self.base_url
            )
            try:
                self.uses_basic_auth_fallback = True
                self._verify_basic_auth()
                return
            except RedfishAuthError:
                self.uses_basic_auth_fallback = False
                raise RedfishAuthError(f"Authentication rejected by {self.base_url}")

        if resp.status_code not in (200, 201):
            raise RedfishUnreachableError(
                f"Unexpected status {resp.status_code} creating session on {self.base_url}"
            )

        token = resp.headers.get("X-Auth-Token")
        location = resp.headers.get("Location") or resp.json().get("@odata.id")
        if not token:
            raise RedfishAuthError(f"No X-Auth-Token returned by {self.base_url}")

        self.token = token
        self.session_uri = self._normalize_location(location)
        self.expires_at = datetime.utcnow() + timedelta(minutes=DEFAULT_ASSUMED_SESSION_TTL_MINUTES)
        logger.info("Authenticated session for %s at %s", self.base_url, self.session_uri)

    def _verify_basic_auth(self):
        """Sanity-check that Basic Auth actually works before we commit to it."""
        for attempt in range(2):
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    auth=(self.username, self.password),
                    verify=_build_ssl_context(self.config.REDFISH_VERIFY_TLS, self.uses_legacy_tls),
                    timeout=self.config.REDFISH_HTTP_TIMEOUT,
                    follow_redirects=True,
                ) as client:
                    resp = client.get("/redfish/v1/Systems")
                break
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as exc:
                formatted = format_httpx_exception(exc)
                # Same legacy TLS retry logic as _authenticate(): use _is_ssl_error()
                # to detect all OpenSSL handshake error codes, not just "SSL"/"certificate".
                if attempt == 0 and not self.uses_legacy_tls and _is_ssl_error(exc):
                    logger.warning(
                        "TLS handshake failed on %s (%s). "
                        "Retrying Basic Auth with legacy TLS (TLS 1.0/1.1, SECLEVEL=0).",
                        self.base_url, formatted,
                    )
                    self.uses_legacy_tls = True
                    continue
                raise RedfishUnreachableError(formatted) from exc
        if resp.status_code in (401, 403):
            raise RedfishAuthError(f"Basic auth rejected by {self.base_url}")
        self.expires_at = None  # basic auth never expires

    def _normalize_location(self, location: str | None) -> str | None:
        if not location:
            return None
        if location.startswith("http"):
            # Strip scheme+host, keep path only.
            from urllib.parse import urlparse
            return urlparse(location).path
        return location


class SessionManager:
    """Process-wide cache of RedfishSession objects, one per server, so
    poller threads reuse authenticated sessions instead of re-logging in
    on every 30-second poll cycle."""

    def __init__(self, config):
        self.config = config
        self._sessions: dict[str, RedfishSession] = {}
        self._lock = threading.Lock()

    def get_session(self, server_id: str, base_url: str, username: str, password: str) -> RedfishSession:
        with self._lock:
            existing = self._sessions.get(server_id)
            if existing and existing.base_url == base_url.rstrip("/"):
                # If credentials have changed (e.g. updated in the UI), we must
                # completely invalidate the old session state, tokens, and basic
                # auth fallback flags so we start fresh.
                if existing.username != username or existing.password != password:
                    existing.logout()
                    self._sessions.pop(server_id, None)
                    existing = None
                else:
                    return existing
            
            session = RedfishSession(base_url, username, password, self.config, server_id)
            self._sessions[server_id] = session
            return session

    def drop_session(self, server_id: str):
        with self._lock:
            session = self._sessions.pop(server_id, None)
        if session:
            session.logout()
