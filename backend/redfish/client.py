"""
redfish/client.py
==================
Thin, resilient GET/POST wrapper around a RedfishSession. All Redfish
traffic in the application flows through this client so retry, timeout,
and re-authentication behavior is consistent everywhere (discovery,
inventory, and every collector).

Nothing in this file is vendor-specific. Vendor differences are handled by
consumers reading whatever properties happen to exist in the JSON body.
"""
import logging
import time

import httpx

from .session import RedfishSession, RedfishAuthError, RedfishUnreachableError, format_httpx_exception

logger = logging.getLogger(__name__)


class RedfishClient:
    def __init__(self, session: RedfishSession, config):
        self.session = session
        self.config = config

    def get(self, path: str) -> dict | None:
        """GET a Redfish resource and return its parsed JSON body, or None
        if the resource does not exist (404) - many Redfish resources are
        genuinely optional and a 404 there is not an error condition."""
        return self._request("GET", path)

    def post(self, path: str, json_body: dict | None = None) -> dict | None:
        return self._request("POST", path, json_body=json_body)

    def post_for_response(self, path: str, json_body: dict | None = None) -> httpx.Response | None:
        """POST and retain response headers for asynchronous Redfish actions."""
        return self._request_response("POST", path, json_body=json_body)

    def patch(self, path: str, json_body: dict) -> dict | None:
        return self._request("PATCH", path, json_body=json_body)

    def get_binary(self, path: str) -> bytes | None:
        """GET a non-JSON Redfish resource, such as a support bundle."""
        resp = self._request_response("GET", path)
        if resp is None or resp.status_code >= 400:
            return None
        return resp.content

    # -- internals ------------------------------------------------------

    def _request(self, method: str, path: str, json_body: dict | None = None) -> dict | None:
        attempts = 0
        last_exc_str = ""
        
        # httpx internally parses the URI and treats '%23' as a literal '#' fragment
        # delimiter, silently truncating everything after it. Dell uses '%23' heavily
        # in their URIs (e.g. ...%23DIMMSLOTA1). We double-encode it to '%2523' so
        # httpx sends the literal '%23' over the wire.
        safe_path = path.replace("%23", "%2523")
        
        while attempts < self.config.REDFISH_MAX_RETRIES:
            attempts += 1
            try:
                client = self.session.get_http_client()
                resp = client.request(method, safe_path, json=json_body)

                if resp.status_code == 404:
                    return None

                if resp.status_code == 501:
                    # HTTP 501 Not Implemented: the BMC firmware does not support
                    # this endpoint (common on Dell iDRAC 8 for newer schema paths).
                    # Treat as "resource not available" — same as 404 — rather than
                    # a server error, to avoid triggering retry backoff storms.
                    logger.debug(
                        "HTTP 501 (Not Implemented) from %s%s — endpoint unsupported on this firmware, skipping",
                        self.session.base_url, path,
                    )
                    return None

                if resp.status_code == 401:
                    # Session expired / was invalidated on the BMC side (e.g.
                    # another client logged the session out, or it idled
                    # out). Force re-authentication and retry once.
                    logger.info("401 from %s on %s - reauthenticating", self.session.base_url, path)
                    self.session.invalidate()
                    if self.session.uses_basic_auth_fallback:
                        raise RedfishAuthError(f"Basic auth rejected by {self.session.base_url}")
                    continue

                if resp.status_code >= 500:
                    # A 5xx from a specific endpoint means that BMC resource is
                    # broken or unsupported on this firmware — NOT that the whole
                    # BMC is unreachable. Raising RedfishUnreachableError here
                    # would invalidate the session and force re-auth on every
                    # retry (observed on Lenovo XCC /BootSettings/* endpoints).
                    # Instead, log and retry without session invalidation; after
                    # exhausting retries return None (same as 404) so the
                    # collector skips this resource gracefully.
                    last_exc_str = f"HTTP {resp.status_code} from {path}"
                    wait = self.config.REDFISH_RETRY_BACKOFF_SECONDS * attempts
                    logger.warning(
                        "HTTP %s from %s%s — endpoint may be unsupported on this firmware "
                        "(%s/%s), retrying in %.1fs",
                        resp.status_code, self.session.base_url, path,
                        attempts, self.config.REDFISH_MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue

                # Any other 4xx (403 Forbidden, 405 Method Not Allowed, etc.)
                # means "resource not accessible to this account" — return None
                # rather than raising, so collectors can silently skip optional
                # resources without crashing the entire category.
                if resp.status_code >= 400:
                    logger.debug(
                        "HTTP %s from %s%s - resource not accessible, skipping",
                        resp.status_code, self.session.base_url, path,
                    )
                    return None

                resp.raise_for_status()
                if not resp.content:
                    return {}
                return resp.json()

            except httpx.RequestError as exc:
                # Only true network-level failures (TCP, DNS, TLS) should
                # invalidate the session.  BMC-side HTTP errors are handled
                # above without touching the session.
                self.session.invalidate()
                last_exc_str = format_httpx_exception(exc)
                wait = self.config.REDFISH_RETRY_BACKOFF_SECONDS * attempts
                logger.warning(
                    "Redfish request failed (%s/%s) for %s%s: %s - retrying in %.1fs",
                    attempts, self.config.REDFISH_MAX_RETRIES, self.session.base_url, path, last_exc_str, wait,
                )
                time.sleep(wait)
            except RedfishAuthError:
                raise

        # Exhausted retries — if the last error was a 5xx, treat as 'resource
        # unavailable' rather than 'BMC unreachable' so one bad endpoint does
        # not abort the entire poll cycle for this server.
        if last_exc_str.startswith("HTTP 5"):
            logger.warning(
                "Giving up on %s%s after %s retries (persistent 5xx) — skipping resource",
                self.session.base_url, path, self.config.REDFISH_MAX_RETRIES,
            )
            return None
        raise RedfishUnreachableError(
            f"Exhausted retries reaching {self.session.base_url}{path}: {last_exc_str}"
        )

    def _request_response(
        self, method: str, path: str, json_body: dict | None = None,
    ) -> httpx.Response | None:
        """Issue a request while preserving headers and binary content.

        This is intentionally limited to callers that need a raw Redfish
        response. JSON collectors continue to use ``_request`` above.
        """
        attempts = 0
        last_exc_str = ""
        safe_path = path.replace("%23", "%2523")
        
        while attempts < self.config.REDFISH_MAX_RETRIES:
            attempts += 1
            try:
                client = self.session.get_http_client()
                resp = client.request(method, safe_path, json=json_body)

                if resp.status_code == 401:
                    logger.info("401 from %s on %s - reauthenticating", self.session.base_url, path)
                    self.session.invalidate()
                    if self.session.uses_basic_auth_fallback:
                        raise RedfishAuthError(f"Basic auth rejected by {self.session.base_url}")
                    continue

                if resp.status_code == 501:
                    # Same as in _request(): 501 means the endpoint is unsupported
                    # on this firmware — return None rather than raising / retrying.
                    logger.debug(
                        "HTTP 501 (Not Implemented) from %s%s — endpoint unsupported on this firmware, skipping",
                        self.session.base_url, path,
                    )
                    return None

                if resp.status_code >= 500:
                    # Same logic as _request(): 5xx on a specific endpoint means
                    # the resource is broken/unsupported, not the whole BMC.
                    # Do not invalidate the session; just retry then return None.
                    last_exc_str = f"HTTP {resp.status_code} from {path}"
                    wait = self.config.REDFISH_RETRY_BACKOFF_SECONDS * attempts
                    logger.warning(
                        "HTTP %s from %s%s — endpoint may be unsupported (%s/%s), retrying in %.1fs",
                        resp.status_code, self.session.base_url, path,
                        attempts, self.config.REDFISH_MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue

                return resp

            except httpx.RequestError as exc:
                # Only network-level failures invalidate the session.
                self.session.invalidate()
                last_exc_str = format_httpx_exception(exc)
                wait = self.config.REDFISH_RETRY_BACKOFF_SECONDS * attempts
                logger.warning(
                    "Redfish request failed (%s/%s) for %s%s: %s - retrying in %.1fs",
                    attempts, self.config.REDFISH_MAX_RETRIES, self.session.base_url, path,
                    last_exc_str, wait,
                )
                time.sleep(wait)
            except RedfishAuthError:
                raise

        if last_exc_str.startswith("HTTP 5"):
            logger.warning(
                "Giving up on %s%s after %s retries (persistent 5xx) — skipping resource",
                self.session.base_url, path, self.config.REDFISH_MAX_RETRIES,
            )
            return None
        raise RedfishUnreachableError(
            f"Exhausted retries reaching {self.session.base_url}{path}: {last_exc_str}"
        )
