"""
agent.py
========
Standalone Collector Agent for Redfish Fleet Monitor.
Runs on remote sites to collect telemetry from local BMCs and pushes it to the Central Server.

Architecture
------------
- Reads agent_config.json at startup (auto-creates a template on first run).
- Polls each configured BMC device on its own interval via BackgroundScheduler +
  ThreadPoolExecutor.
- Stores collected payloads in a local SQLite queue (agent_queue.db).
- A dedicated push job drains the queue to the Central Server with exponential
  backoff when the server is unavailable.
- Oversized / permanently-failed payloads (HTTP 4xx) are dropped so a bad
  payload never permanently blocks the queue.
- The queue is capped at MAX_QUEUE_SIZE entries; the oldest entries are evicted
  when over the limit to bound memory and disk usage.
- Entries older than QUEUE_MAX_AGE_SECONDS are pruned periodically.
- All SQLite access is serialized with a threading.Lock.
- Clean shutdown on SIGINT/SIGTERM: scheduler, executor, and SQLite connection
  are all properly closed.
"""
import json
import logging
import math
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import httpx
from apscheduler.schedulers.background import BackgroundScheduler

# Import existing modules
from redfish.session import SessionManager, RedfishAuthError, RedfishUnreachableError
from redfish.client import RedfishClient
from redfish import inventory, discovery
from redfish.collectors import COLLECTOR_REGISTRY
from redfish.collectors import logs as logs_collector

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] Agent: %(message)s",
)
logger = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_PATH = Path("agent_config.json")
QUEUE_DB_PATH = Path("agent_queue.db")

# Default Central Server URL — baked into the agent so customers
# do not need to enter it manually.  Can be overridden in agent_config.json.
DEFAULT_CENTRAL_SERVER_URL = "https://navifix.in"


# ---------------------------------------------------------------------------
# Lightweight config dict with attribute access
# ---------------------------------------------------------------------------
class AgentConfig(dict):
    def __getattr__(self, name):
        return self.get(name)


# ---------------------------------------------------------------------------
# Dummy DB session — inventory.refresh_inventory() writes server_row fields
# directly onto the Python object and then calls db_session.add/commit.  The
# agent has no real DB; this stub satisfies the interface so the attribute
# mutations (vendor, model, …) still land on the ServerStub object while
# silently ignoring the persistence calls.
# ---------------------------------------------------------------------------
class DummyDBSession:
    def add(self, obj):
        pass

    def commit(self):
        pass


# ---------------------------------------------------------------------------
# ServerStub — minimal server object passed into existing redfish modules
# ---------------------------------------------------------------------------
class ServerStub:
    """Mock Server object to pass into existing redfish modules."""

    def __init__(self, data):
        self.id = data["server_id"]
        self.hostname = data["ip_address"]   # use IP as hostname for the agent
        self.ip_address = data["ip_address"]
        self.username = data["username"]
        self._password = data["password"]

        # Identity fields populated by inventory.refresh_inventory()
        self.redfish_service_root = None
        self.redfish_system_uri = None
        self.redfish_chassis_uri = None
        self.redfish_manager_uri = None
        self.vendor = None
        self.model = None
        self.serial_number = None
        self.asset_tag = None
        self.service_tag = None
        self.power_state = None
        self.health_status = None
        self.firmware_version = None
        self.supports_event_service = None

        # Required by inventory.refresh_inventory() but not used by agent
        self.updated_at = None

    def to_inventory_dict(self):
        return {
            "redfish_service_root": self.redfish_service_root,
            "redfish_system_uri": self.redfish_system_uri,
            "redfish_chassis_uri": self.redfish_chassis_uri,
            "redfish_manager_uri": self.redfish_manager_uri,
            "vendor": self.vendor,
            "model": self.model,
            "serial_number": self.serial_number,
            "asset_tag": self.asset_tag,
            "service_tag": self.service_tag,
            "power_state": self.power_state,
            "health_status": self.health_status,
            "firmware_version": self.firmware_version,
            "supports_event_service": self.supports_event_service,
        }


# ---------------------------------------------------------------------------
# CollectorAgent
# ---------------------------------------------------------------------------
class CollectorAgent:

    # Queue limits
    MAX_QUEUE_SIZE = 500            # max rows kept in the local queue
    QUEUE_MAX_AGE_SECONDS = 3600    # purge entries older than this (1 hour)

    # Push back-off: wait = min(2^n, MAX_PUSH_BACKOFF) seconds between retries
    MAX_PUSH_BACKOFF = 300          # 5 minutes ceiling

    def __init__(self):
        self.load_config()

        # Auto-enroll if no agent token exists yet
        if not self.config.get("AGENT_TOKEN"):
            self.enroll()

        self._db_lock = threading.Lock()   # serialize ALL SQLite access
        self.init_db()
        self.scheduler = BackgroundScheduler(daemon=True)
        self.executor = ThreadPoolExecutor(
            max_workers=self.config.get("MAX_CONCURRENT_POLLS", 5)
        )

        # Build Redfish config from agent_config.json overrides + sensible defaults
        self.rf_config = AgentConfig({
            "REDFISH_VERIFY_TLS": self.config.get("REDFISH_VERIFY_TLS", False),
            "REDFISH_HTTP_TIMEOUT": self.config.get("REDFISH_HTTP_TIMEOUT", 30),
            "REDFISH_MAX_RETRIES": self.config.get("REDFISH_MAX_RETRIES", 3),
            "REDFISH_RETRY_BACKOFF_SECONDS": self.config.get("REDFISH_RETRY_BACKOFF_SECONDS", 2.0),
            "REDFISH_SESSION_REFRESH_MARGIN": timedelta(minutes=5),
            "INVENTORY_REFRESH_INTERVAL_SECONDS": self.config.get(
                "INVENTORY_REFRESH_INTERVAL_SECONDS", 3600
            ),
        })
        self.session_manager = SessionManager(self.rf_config)

        self._topology_cache = {}
        self._last_inventory_refresh = {}

        # Push back-off state (not shared across threads — only the push job
        # calls _push_queue_internal, so no lock needed here).
        self._push_fail_count = 0
        self._next_push_allowed_at = 0.0   # epoch seconds

    # -----------------------------------------------------------------------
    # Configuration
    # -----------------------------------------------------------------------

    def load_config(self):
        if not CONFIG_PATH.exists():
            default_config = {
                "ENROLLMENT_KEY": "paste-enrollment-key-from-company",
                "AGENT_NAME": "CustomerName-SiteName",
                "CUSTOMER_NAME": "",
                "CUSTOMER_LOCATION": "",
                "CENTRAL_SERVER_URL": DEFAULT_CENTRAL_SERVER_URL,
                "CENTRAL_SERVER_VERIFY_TLS": True,
                "REDFISH_VERIFY_TLS": False,
                "REDFISH_HTTP_TIMEOUT": 30,
                "REDFISH_MAX_RETRIES": 3,
                "REDFISH_RETRY_BACKOFF_SECONDS": 2.0,
                "POLL_INTERVAL_SECONDS": 30,
                "MAX_CONCURRENT_POLLS": 5,
                "INVENTORY_REFRESH_INTERVAL_SECONDS": 3600,
                "DEVICES": [
                    {
                        "ip_address": "192.168.1.100",
                        "username": "root",
                        "password": "password",
                        "hostname": "server-01",
                    }
                ],
            }
            CONFIG_PATH.write_text(json.dumps(default_config, indent=2))
            logger.info(
                "Created default agent_config.json — edit it with your "
                "enrollment key and BMC device details, then restart."
            )
            exit(1)

        with open(CONFIG_PATH) as f:
            self.config = json.load(f)

        # Apply default Central Server URL if not in config
        if "CENTRAL_SERVER_URL" not in self.config or not self.config["CENTRAL_SERVER_URL"]:
            self.config["CENTRAL_SERVER_URL"] = DEFAULT_CENTRAL_SERVER_URL

    # -----------------------------------------------------------------------
    # Auto-enrollment
    # -----------------------------------------------------------------------

    def enroll(self):
        """Register this agent with the Central Server.

        Sends the enrollment key + device list to POST /api/agents/enroll.
        On success, saves the returned agent_token and server_id mappings
        back into agent_config.json so subsequent runs skip enrollment.
        """
        enrollment_key = self.config.get("ENROLLMENT_KEY", "")
        if not enrollment_key or enrollment_key == "paste-enrollment-key-from-company":
            logger.error(
                "ENROLLMENT_KEY is not set in agent_config.json. "
                "Get the enrollment key from the company and paste it in."
            )
            exit(1)

        base_url = self.config.get("CENTRAL_SERVER_URL", DEFAULT_CENTRAL_SERVER_URL)
        url = f"{base_url.rstrip('/')}/api/agents/enroll"
        verify_tls = self.config.get("CENTRAL_SERVER_VERIFY_TLS", True)

        payload = {
            "enrollment_key": enrollment_key,
            "agent_name": self.config.get("AGENT_NAME", ""),
            "customer_name": self.config.get("CUSTOMER_NAME", ""),
            "customer_location": self.config.get("CUSTOMER_LOCATION", ""),
            "devices": self.config.get("DEVICES", []),
        }

        logger.info("Enrolling with Central Server at %s ...", base_url)
        try:
            resp = httpx.post(url, json=payload, verify=verify_tls, timeout=30)
        except Exception as exc:
            logger.error("Failed to reach Central Server for enrollment: %s", exc)
            logger.error("Check CENTRAL_SERVER_URL and network connectivity.")
            exit(1)

        if resp.status_code == 201:
            result = resp.json()
            logger.info("✅ Enrollment successful! Agent ID: %s", result["agent_id"])

            # Save token and server_id mappings back to config
            self.config["AGENT_TOKEN"] = result["agent_token"]
            self.config["AGENT_ID"] = result["agent_id"]

            # Update DEVICES with server_ids from the Central Server
            returned_devices = {d["ip_address"]: d["server_id"] for d in result.get("devices", [])}
            for dev in self.config.get("DEVICES", []):
                ip = dev.get("ip_address", "")
                if ip in returned_devices:
                    dev["server_id"] = returned_devices[ip]

            # Persist updated config
            CONFIG_PATH.write_text(json.dumps(self.config, indent=2))
            logger.info("Updated agent_config.json with token and server IDs.")
        elif resp.status_code == 409:
            logger.error("Agent name '%s' is already registered. Use a unique AGENT_NAME.", self.config.get("AGENT_NAME"))
            exit(1)
        elif resp.status_code == 403:
            logger.error("Invalid enrollment key. Check ENROLLMENT_KEY in agent_config.json.")
            exit(1)
        else:
            logger.error("Enrollment failed: %s %s", resp.status_code, resp.text[:500])
            exit(1)

    # -----------------------------------------------------------------------
    # SQLite queue  (ALL methods acquire self._db_lock before touching self.conn)
    # -----------------------------------------------------------------------

    def init_db(self):
        # isolation_level=None → autocommit; we manage transactions explicitly.
        self.conn = sqlite3.connect(
            QUEUE_DB_PATH, check_same_thread=False, isolation_level=None
        )
        # Write-Ahead Logging: better concurrent read performance and crash safety.
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self._db_lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def _queue_size(self) -> int:
        """Return current number of rows in the queue. Caller must hold _db_lock."""
        row = self.conn.execute("SELECT COUNT(*) FROM queue").fetchone()
        return row[0] if row else 0

    def enqueue_payload(self, payload: dict):
        """Insert payload into the local queue.

        If the queue is at MAX_QUEUE_SIZE, the oldest entry is evicted first
        (a warning is logged so operators know data was dropped).
        """
        with self._db_lock:
            size = self._queue_size()
            if size >= self.MAX_QUEUE_SIZE:
                # Evict oldest to make room for fresh data.
                self.conn.execute(
                    "DELETE FROM queue WHERE id = (SELECT MIN(id) FROM queue)"
                )
                logger.warning(
                    "Queue at capacity (%d); evicted oldest entry to make room for new payload.",
                    self.MAX_QUEUE_SIZE,
                )
            self.conn.execute(
                "INSERT INTO queue (payload) VALUES (?)", (json.dumps(payload),)
            )

    def push_queue(self):
        """Scheduler entry-point: respects exponential back-off when the
        central server is down, then delegates to _push_queue_internal()."""
        now = time.monotonic()
        if now < self._next_push_allowed_at:
            remaining = self._next_push_allowed_at - now
            logger.debug("Push back-off active — %.0fs remaining.", remaining)
            return
        self._push_queue_internal()

    def _push_queue_internal(self):
        """Drain up to 50 queued payloads toward the Central Server.

        Back-off policy
        ---------------
        - On any network / 5xx error: exponential back-off, up to MAX_PUSH_BACKOFF.
        - On HTTP 4xx: the payload is **deleted** (it will never succeed) and
          processing continues — a bad payload must never permanently block the queue.
        - On success: back-off counter resets.
        """
        with self._db_lock:
            cursor = self.conn.execute(
                "SELECT id, payload FROM queue ORDER BY id ASC LIMIT 50"
            )
            rows = cursor.fetchall()

        if not rows:
            return

        headers = {"X-Agent-Token": self.config["AGENT_TOKEN"]}
        verify_tls = self.config.get("CENTRAL_SERVER_VERIFY_TLS", False)
        url = f"{self.config['CENTRAL_SERVER_URL']}/api/ingest/telemetry"

        for row_id, payload_str in rows:
            try:
                payload = json.loads(payload_str)
                resp = httpx.post(
                    url,
                    json=payload,
                    headers=headers,
                    verify=verify_tls,
                    timeout=15,
                )

                if resp.status_code in range(400, 500):
                    # Client error (bad token, server not assigned to agent, etc.)
                    # — the payload cannot succeed; delete it and keep going.
                    logger.warning(
                        "Dropping queued payload %d: Central Server returned %s (%s).",
                        row_id, resp.status_code, resp.text[:200],
                    )
                    with self._db_lock:
                        self.conn.execute("DELETE FROM queue WHERE id = ?", (row_id,))
                    continue

                resp.raise_for_status()   # raises on 5xx

                # Success
                with self._db_lock:
                    self.conn.execute("DELETE FROM queue WHERE id = ?", (row_id,))

                # Reset back-off on first successful push.
                if self._push_fail_count > 0:
                    logger.info("Central Server reachable again — resetting push back-off.")
                    self._push_fail_count = 0
                    self._next_push_allowed_at = 0.0

            except Exception as exc:
                self._push_fail_count += 1
                backoff = min(2 ** self._push_fail_count, self.MAX_PUSH_BACKOFF)
                self._next_push_allowed_at = time.monotonic() + backoff
                logger.error(
                    "Failed to push payload %d to Central Server (attempt %d): %s. "
                    "Next retry in %ds.",
                    row_id, self._push_fail_count, exc, backoff,
                )
                # Stop pushing — server is down; wait for back-off to expire.
                break

    def prune_old_queue(self):
        """Remove queue entries older than QUEUE_MAX_AGE_SECONDS.

        Called periodically so that a prolonged central-server outage doesn't
        cause the agent to replay hours of stale state data when connectivity
        is restored.
        """
        cutoff = datetime.utcnow() - timedelta(seconds=self.QUEUE_MAX_AGE_SECONDS)
        with self._db_lock:
            deleted = self.conn.execute(
                "DELETE FROM queue WHERE created_at < ?", (cutoff.isoformat(),)
            ).rowcount
        if deleted:
            logger.info("Pruned %d stale queue entries older than %ds.", deleted, self.QUEUE_MAX_AGE_SECONDS)

    def send_heartbeat(self):
        """Send a lightweight heartbeat to the Central Server."""
        token = self.config.get("AGENT_TOKEN")
        if not token:
            return
        base_url = self.config.get("CENTRAL_SERVER_URL", DEFAULT_CENTRAL_SERVER_URL)
        url = f"{base_url.rstrip('/')}/api/agents/heartbeat"
        verify_tls = self.config.get("CENTRAL_SERVER_VERIFY_TLS", False)
        try:
            resp = httpx.post(
                url,
                headers={"X-Agent-Token": token},
                verify=verify_tls,
                timeout=10,
            )
            if resp.status_code != 200:
                logger.debug("Heartbeat returned %d", resp.status_code)
        except Exception as exc:
            logger.debug("Heartbeat failed: %s", exc)

    # -----------------------------------------------------------------------
    # Topology / inventory
    # -----------------------------------------------------------------------

    def get_topology(self, client, server_stub):
        now = datetime.utcnow()
        last_refresh = self._last_inventory_refresh.get(server_stub.id)
        if (
            server_stub.id not in self._topology_cache
            or last_refresh is None
            or (now - last_refresh).total_seconds()
                > self.rf_config["INVENTORY_REFRESH_INTERVAL_SECONDS"]
        ):
            topology = inventory.refresh_inventory(client, server_stub, DummyDBSession())
            self._topology_cache[server_stub.id] = topology
            self._last_inventory_refresh[server_stub.id] = now
            return topology, True
        return self._topology_cache[server_stub.id], False

    # -----------------------------------------------------------------------
    # Serialisation helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _serialize_component(c):
        return {
            "category": c["category"].value if hasattr(c["category"], "value") else c["category"],
            "odata_id": c["odata_id"],
            "name": c["name"],
            "health": c["health"],
            "state": c["state"],
            "location": c["location"],
            "raw_json": c["raw_json"],
        }

    # -----------------------------------------------------------------------
    # Per-device poll
    # -----------------------------------------------------------------------

    def poll_device(self, device_config):
        server = ServerStub(device_config)
        logger.info("Polling %s …", server.ip_address)

        payload = {
            "server_id": server.id,
            "components": {},
            "readings": [],
            "logs": [],
        }

        base_url = f"https://{server.ip_address}"
        try:
            redfish_session = self.session_manager.get_session(
                server.id, base_url, server.username, server._password
            )
            client = RedfishClient(redfish_session, self.rf_config)
        except RedfishAuthError as exc:
            logger.error("Auth failed for %s: %s", server.ip_address, exc)
            payload["connection_status"] = "auth_failed"
            payload["connection_error"] = str(exc)
            self.enqueue_payload(payload)
            return
        except Exception as exc:
            logger.error("Cannot establish session for %s: %s", server.ip_address, exc)
            payload["connection_status"] = "unreachable"
            payload["connection_error"] = str(exc)
            self.enqueue_payload(payload)
            return

        try:
            topology, inventory_updated = self.get_topology(client, server)
            if inventory_updated:
                payload["inventory"] = server.to_inventory_dict()
        except RedfishAuthError:
            payload["connection_status"] = "auth_failed"
            payload["connection_error"] = "Authentication failed during inventory"
            self.enqueue_payload(payload)
            return
        except RedfishUnreachableError as exc:
            payload["connection_status"] = "unreachable"
            payload["connection_error"] = str(exc)
            self.enqueue_payload(payload)
            return
        except Exception as exc:
            payload["connection_status"] = "unreachable"
            payload["connection_error"] = str(exc)
            self.enqueue_payload(payload)
            return

        payload["connection_status"] = "connected"

        # -- collect hardware components ------------------------------------
        for cat_name, collector in COLLECTOR_REGISTRY.items():
            try:
                components, readings = collector.collect(client, server, topology)
                payload["components"][cat_name] = [
                    self._serialize_component(c) for c in components
                ]
                payload["readings"].extend(readings)
            except Exception as exc:
                logger.error("Collector '%s' failed for %s: %s", cat_name, server.ip_address, exc)

        # -- collect logs ---------------------------------------------------
        try:
            logs = logs_collector.collect(client, server, topology)
            payload["logs"].extend(logs)
        except Exception as exc:
            logger.error("Log collection failed for %s: %s", server.ip_address, exc)

        self.enqueue_payload(payload)
        logger.debug("Payload enqueued for %s (%d components, %d readings, %d logs).",
                     server.ip_address,
                     sum(len(v) for v in payload["components"].values()),
                     len(payload["readings"]),
                     len(payload["logs"]))

    # -----------------------------------------------------------------------
    # Poll cycle
    # -----------------------------------------------------------------------

    def poll_all(self):
        """Submit all configured devices for polling and wait for completion.

        The push_queue job runs independently every 10 seconds — we intentionally
        do NOT call push_queue() here so that slow / unreachable devices cannot
        delay the delivery of telemetry from faster, already-completed devices.
        """
        logger.info("Starting poll cycle …")
        devices = self.config.get("DEVICES", [])
        futures = [self.executor.submit(self.poll_device, dev) for dev in devices]
        for f in futures:
            try:
                f.result()
            except Exception as exc:
                logger.error("Unhandled error in poll thread: %s", exc)

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def run(self):
        interval = self.config.get("POLL_INTERVAL_SECONDS", 30)

        # Main poll job — fires every `interval` seconds
        self.scheduler.add_job(
            self.poll_all,
            "interval",
            seconds=interval,
            id="poll_all",
            max_instances=1,
            coalesce=True,    # skip missed firings instead of piling up
        )

        # Push job — drains queue every 10 seconds (with its own back-off logic)
        self.scheduler.add_job(
            self.push_queue,
            "interval",
            seconds=10,
            id="push_queue",
            max_instances=1,
            coalesce=True,
        )

        # Queue maintenance — purge entries older than QUEUE_MAX_AGE_SECONDS
        self.scheduler.add_job(
            self.prune_old_queue,
            "interval",
            minutes=15,
            id="prune_queue",
            max_instances=1,
        )

        # Heartbeat — let Central Server know we are alive even when
        # there is no telemetry to push (e.g., all BMCs are down).
        self.scheduler.add_job(
            self.send_heartbeat,
            "interval",
            seconds=60,
            id="heartbeat",
            replace_existing=True,
        )

        self.scheduler.start()
        logger.info(
            "Collector Agent started. Polling %d device(s) every %ds. "
            "Queue cap: %d entries / %ds max age.",
            len(self.config.get("DEVICES", [])),
            interval,
            self.MAX_QUEUE_SIZE,
            self.QUEUE_MAX_AGE_SECONDS,
        )

        # Run first poll immediately so there is no `interval`-second gap at startup
        self.poll_all()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down …")
        finally:
            self.scheduler.shutdown(wait=False)
            self.executor.shutdown(wait=True)
            with self._db_lock:
                self.conn.close()
            logger.info("Agent stopped cleanly.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    agent = CollectorAgent()
    agent.run()