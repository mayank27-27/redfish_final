"""
storage_devices/msa_client.py
======================
Client for HPE MSA storage arrays (MSA 1050/2040/2050/2052 etc).

These do NOT speak functional Redfish - the standard SessionService POST
returns 501/404 on this firmware. MSA instead exposes its own long-lived
native REST API:

  1. Login:  GET /api/login/<hash>
             where <hash> = MD5("<username>_<password>")
             Response (XML) contains a PROPERTY name="response" holding
             the session key.
  2. Every other call: GET /api/show/<object>
             with header "sessionKey: <key>"
  3. Sessions expire after ~15-30 min of inactivity (undocumented exact
     value on this firmware) - we proactively re-login on any 401-shaped
     failure rather than trying to track a TTL.

Response bodies are XML in this shape (confirmed against a real MSA 2040):

    <RESPONSE VERSION="L100" REQUEST="show disks">
      <OBJECT basetype="drives" name="drive" oid="1" format="rows">
        <PROPERTY name="health" ...>OK</PROPERTY>
        <PROPERTY name="serial-number" ...>6680A01LF6TA</PROPERTY>
        ...
        <OBJECT basetype="..." ...>   <!-- OBJECTs can nest -->
          ...
        </OBJECT>
      </OBJECT>
      <OBJECT basetype="drives" name="drive" oid="2" format="rows">
        ...
      </OBJECT>
    </RESPONSE>

Every OBJECT becomes one dict of {property-name: text-value}, plus a
"_basetype" key and a "_children" list for nested OBJECTs. This is
generic - the same parser works for show/disks, show/controllers,
show/system, show/pools, show/volumes, etc.
"""
import hashlib
import logging
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger(__name__)


class MSAAuthError(Exception):
    """Login rejected - bad credentials."""


class MSAUnreachableError(Exception):
    """Could not reach the array at all (network/TLS/timeout)."""


def _login_hash(username: str, password: str) -> str:
    raw = f"{username}_{password}"
    return hashlib.md5(raw.encode()).hexdigest()


def _parse_object(el: ET.Element) -> dict:
    """Flatten one <OBJECT> element into a dict. Direct child <PROPERTY>
    elements become {name: text}. Direct child <OBJECT> elements are
    collected under "_children" (each parsed recursively) rather than
    merged into the same dict, since sibling OBJECTs can reuse property
    names (e.g. every controller has its own "health")."""
    obj = {"_basetype": el.get("basetype"), "_name": el.get("name")}
    children = []
    for child in el:
        if child.tag == "PROPERTY":
            obj[child.get("name")] = (child.text or "").strip()
        elif child.tag == "OBJECT":
            children.append(_parse_object(child))
    if children:
        obj["_children"] = children
    return obj


def parse_msa_response(xml_text: str) -> list[dict]:
    """Parse a full MSA <RESPONSE> body into a list of top-level OBJECT
    dicts (one per <OBJECT> directly under <RESPONSE> - e.g. one per
    disk, one per controller)."""
    root = ET.fromstring(xml_text)
    return [_parse_object(obj) for obj in root.findall("OBJECT")]


class MSAClient:
    """One instance per storage device. Not thread-safe - matches the
    rest of this codebase's per-server client pattern (session.py)."""

    def __init__(self, base_url: str, username: str, password: str, config):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.config = config
        self.session_key: str | None = None

    def _http_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            verify=getattr(self.config, "REDFISH_VERIFY_TLS", False),
            timeout=getattr(self.config, "REDFISH_HTTP_TIMEOUT", 30),
        )

    def login(self):
        h = _login_hash(self.username, self.password)
        try:
            with self._http_client() as c:
                resp = c.get(f"/api/login/{h}")
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MSAUnreachableError(f"Cannot reach {self.base_url}: {exc}") from exc

        if resp.status_code != 200:
            raise MSAUnreachableError(f"Unexpected status {resp.status_code} logging into {self.base_url}")

        objs = parse_msa_response(resp.text)
        status = objs[0] if objs else {}
        if status.get("response-type") != "success":
            raise MSAAuthError(f"Login rejected by {self.base_url}: {status.get('response-type', 'unknown error')}")

        self.session_key = status.get("response")
        if not self.session_key:
            raise MSAAuthError(f"No session key returned by {self.base_url}")
        logger.info("Authenticated MSA session for %s", self.base_url)

    def get(self, endpoint: str, _retried: bool = False) -> list[dict]:
        """endpoint like 'show/disks', 'show/controllers', 'show/system'."""
        if not self.session_key:
            self.login()

        try:
            with self._http_client() as c:
                resp = c.get(f"/api/{endpoint.lstrip('/')}", headers={"sessionKey": self.session_key})
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MSAUnreachableError(f"Cannot reach {self.base_url}: {exc}") from exc

        if resp.status_code != 200:
            raise MSAUnreachableError(f"Unexpected status {resp.status_code} on {self.base_url}/api/{endpoint}")

        objs = parse_msa_response(resp.text)

        # A stale/expired session key comes back as a "status" object with
        # an error response-type rather than an HTTP 401 - detect that and
        # transparently re-login once.
        if objs and objs[0].get("_basetype") == "status" and objs[0].get("response-type") != "success":
            if _retried:
                raise MSAAuthError(f"Session rejected twice by {self.base_url} - credentials may have changed")
            logger.info("MSA session expired for %s - re-authenticating", self.base_url)
            self.session_key = None
            return self.get(endpoint, _retried=True)

        return objs            