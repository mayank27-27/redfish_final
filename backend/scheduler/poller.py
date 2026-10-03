"""
scheduler/poller.py
====================
The engine room. For each enabled Server row, on its own
polling_interval_seconds cadence:
  1. Decrypt the stored BMC password with the Fernet cipher.
  2. Get (or create/refresh) a RedfishSession via SessionManager.
  3. Periodically re-run discovery + inventory refresh.
  4. Run every collector in COLLECTOR_REGISTRY.
  5. Upsert Component rows, insert SensorReading rows, evaluate alerts,
     upsert new LogEntry rows.
  6. Update the Server row's connection/health/power-state summary.
  7. Push updates to browsers over WebSocket.

Concurrency is bounded by MAX_CONCURRENT_POLLS via ThreadPoolExecutor.
BMC HTTP calls are I/O-bound so threads (not processes) are appropriate,
and httpx releases the GIL during network waits.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from database import db
from database.models import Server, Component, SensorReading, LogEntry, ConnectionStatus, HealthStatus
from redfish.session import SessionManager, RedfishAuthError, RedfishUnreachableError
from redfish.client import RedfishClient
from redfish import inventory, discovery, events as redfish_events
from redfish.collectors import COLLECTOR_REGISTRY
from redfish.collectors import logs as logs_collector
from alerts import engine as alert_engine
from auth.credentials import get_cipher
from websocket import events as ws_events

logger = logging.getLogger(__name__)

# Process-wide singleton so app.py's _trigger_poll() can reach it.
_polling_engine_instance = None


def _set_engine_instance(engine):
    global _polling_engine_instance
    _polling_engine_instance = engine


def _normalize_health_status(val: str | None) -> HealthStatus | None:
    if not val:
        return None
    s = str(val).strip().lower()
    if s in ("ok", "good", "normal"):
        return HealthStatus.OK
    if s in ("warning", "warn", "degraded", "non-critical"):
        return HealthStatus.WARNING
    if s in ("critical", "crit", "failed", "error", "fatal"):
        return HealthStatus.CRITICAL
    if s in ("unknown", "unavailable"):
        return HealthStatus.UNKNOWN
    try:
        return HealthStatus(val)
    except ValueError:
        return None


class PollingEngine:
    def __init__(self, app, socketio, config=None):
        """
        Parameters
        ----------
        app     : Flask application
        socketio: SocketIO instance
        config  : AppConfig (from config.build_app_config). If omitted,
                  falls back to app.config['REDFISH_CONFIG'] so that callers
                  that already have both work without changes.
        """
        self.app = app
        self.socketio = socketio

        # Accept either an explicit AppConfig or pull it from app.config.
        # AppConfig supports BOTH attribute (.KEY) and dict (['KEY']) access
        # so session.py, client.py and poller.py can all use it safely.
        if config is not None:
            self.config = config
        elif "REDFISH_CONFIG" in app.config:
            self.config = app.config["REDFISH_CONFIG"]
        else:
            # Legacy fallback: should not happen with the current app.py
            logger.warning("REDFISH_CONFIG not found in app.config — using app.config directly")
            self.config = app.config

        self.session_manager = SessionManager(self.config)
        self.executor = ThreadPoolExecutor(max_workers=self.config["MAX_CONCURRENT_POLLS"])
        self.scheduler = BackgroundScheduler(daemon=True)
        self._topology_cache: dict = {}
        self._last_inventory_refresh: dict = {}
        self._active_polls = set()
        self._active_polls_lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------

    def start(self):
        self.scheduler.add_job(
            self._schedule_due_servers,
            "interval",
            seconds=5,
            id="dispatch_loop",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._prune_old_readings,
            "interval",
            hours=6,
            id="prune_readings",
        )
        self.scheduler.start()
        logger.info(
            "Polling engine started (max_concurrent_polls=%s, interval=%ss)",
            self.config["MAX_CONCURRENT_POLLS"],
            self.config["DEFAULT_POLLING_INTERVAL_SECONDS"],
        )

    def shutdown(self):
        self.scheduler.shutdown(wait=False)
        self.executor.shutdown(wait=False)

    # -- dispatch --------------------------------------------------------

    def _schedule_due_servers(self):
        with self.app.app_context():
            now = datetime.utcnow()
            # Only poll servers that are NOT managed by a remote agent
            servers = Server.query.filter_by(enabled=True, agent_id=None).all()
            for server in servers:
                last = server.last_poll_attempt
                interval = server.polling_interval_seconds or self.config["DEFAULT_POLLING_INTERVAL_SECONDS"]
                # Back off AUTH_FAILED servers to 5 minutes to avoid hammering
                # the BMC with repeated failing authentication attempts.
                if server.connection_status == ConnectionStatus.AUTH_FAILED:
                    interval = max(interval, 300)
                if last and (now - last).total_seconds() < interval:
                    continue
                
                server_id_str = str(server.id)
                with self._active_polls_lock:
                    if server_id_str in self._active_polls:
                        logger.warning("Skipping scheduled poll for %s: previous poll still executing", server.hostname)
                        continue
                    self._active_polls.add(server_id_str)
                    
                self.executor.submit(self._poll_server_safe, server.id)

    def poll_server_now(self, server_id: str):
        """Immediate on-demand refresh (called from /api/servers/<id>/poll-now)."""
        with self.app.app_context():
            server = db.session.get(Server, server_id)
            if server and server.agent_id is not None:
                logger.warning("Cannot manual poll server %s directly; it is managed by agent %s", server_id, server.agent_id)
                return

        server_id_str = str(server_id)
        with self._active_polls_lock:
            if server_id_str in self._active_polls:
                logger.warning("Ignoring manual poll request for %s: poll already in progress", server_id_str)
                return
            self._active_polls.add(server_id_str)
            
        self.executor.submit(self._poll_server_safe, server_id)

    def _poll_server_safe(self, server_id: str):
        """Wrapper that catches all exceptions so a broken or deleted server
        never kills the thread pool worker."""
        from sqlalchemy.orm.exc import ObjectDeletedError
        from sqlalchemy.exc import InvalidRequestError

        server_id_str = str(server_id)
        try:
            with self.app.app_context():
                try:
                    self._poll_server(server_id_str)
                except (ObjectDeletedError, InvalidRequestError):
                    logger.warning("Server %s was deleted during poll operation; aborting poll cycle.", server_id_str)
                except Exception:
                    logger.exception("Unhandled error polling server %s", server_id_str)
        finally:
            with self._active_polls_lock:
                self._active_polls.discard(server_id_str)

    # -- core poll cycle ---------------------------------------------------

    def _poll_server(self, server_id: str):
        from sqlalchemy.orm.exc import ObjectDeletedError
        from sqlalchemy.exc import InvalidRequestError

        server_id_str = str(server_id)
        server = db.session.get(Server, server_id_str)
        if not server or not server.enabled:
            return

        try:
            hostname = server.hostname or server_id_str
            ip_address = server.ip_address or "unknown"
            username = server.username
            management_protocol = server.management_protocol
            password_encrypted = server.password_encrypted
        except (ObjectDeletedError, InvalidRequestError):
            logger.warning("Server %s was deleted prior to poll start", server_id_str)
            return

        server.last_poll_attempt = datetime.utcnow()
        db.session.add(server)
        try:
            db.session.commit()
        except (ObjectDeletedError, InvalidRequestError):
            db.session.rollback()
            logger.warning("Server %s was deleted during poll start initialization", server_id_str)
            return

        # Decrypt BMC password. Provide a clear error if the key is wrong.
        if not server.password_encrypted:
            server.connection_status = ConnectionStatus.AUTH_FAILED
            server.last_poll_error = "No password stored for this server."
            db.session.add(server)
            db.session.commit()
            ws_events.emit_server_summary_update(self.socketio, server.to_summary_dict())
            return

        cipher = get_cipher(self.config)
        try:
            password = cipher.decrypt(password_encrypted)
        except ValueError as exc:
            msg = (
                f"Cannot decrypt BMC password for {hostname} ({ip_address}). "
                f"This usually means ENCRYPTION_KEY changed since the server was added. "
                f"Re-add the server via the UI to store its password with the current key. "
                f"Detail: {exc}"
            )
            logger.error(msg)
            self._mark_connection(server, ConnectionStatus.AUTH_FAILED, "Decryption failed — ENCRYPTION_KEY may have changed. Re-add this server.", server_id_str=server_id_str)
            return

        if management_protocol == "ilo2":
            from redfish.ilo2_client import ILO2Client
            client = ILO2Client(ip_address, username, password, self.config)
            topology = {
                "service_root": {"RedfishVersion": "1.0.0"},
                "systems": ["/redfish/v1/Systems/1"],
                "chassis": ["/redfish/v1/Chassis/1"],
                "managers": ["/redfish/v1/Managers/1"],
                "per_system": {
                    "/redfish/v1/Systems/1": {
                        "processors": "/redfish/v1/Systems/1/Processors",
                        "memory": "/redfish/v1/Systems/1/Memory",
                        "storage": "/redfish/v1/Systems/1/Storage",
                        "ethernet_interfaces": "/redfish/v1/Systems/1/EthernetInterfaces",
                        "log_services": "/redfish/v1/Systems/1/LogServices",
                    }
                },
                "per_chassis": {
                    "/redfish/v1/Chassis/1": {
                        "power": "/redfish/v1/Chassis/1/Power",
                        "thermal": "/redfish/v1/Chassis/1/Thermal",
                    }
                },
                "per_manager": {
                    "/redfish/v1/Managers/1": {
                        "ethernet_interfaces": "/redfish/v1/Managers/1/EthernetInterfaces"
                    }
                }
            }
            alert_engine.resolve_connection_alerts(db.session, server_id_str, "auth_failed")
            alert_engine.resolve_connection_alerts(db.session, server_id_str, "unreachable")
        else:
            base_url = f"https://{ip_address}"
            redfish_session = self.session_manager.get_session(server_id_str, base_url, username, password)
            client = RedfishClient(redfish_session, self.config)

            try:
                topology = self._get_topology(client, server)
            except RedfishAuthError:
                self._mark_connection(server, ConnectionStatus.AUTH_FAILED, "Authentication failed", server_id_str=server_id_str)
                try:
                    if db.session.get(Server, server_id_str):
                        alert_engine.raise_connection_alert(
                            db.session, server_id_str, alert_engine.AlertSeverity.CRITICAL,
                            f"Authentication failed for {hostname} ({ip_address})", "auth_failed",
                            config=self.config, server=server,
                        )
                except (ObjectDeletedError, InvalidRequestError):
                    pass
                return
            except RedfishUnreachableError as exc:
                self._mark_connection(server, ConnectionStatus.UNREACHABLE, str(exc), server_id_str=server_id_str)
                try:
                    if db.session.get(Server, server_id_str):
                        alert_engine.raise_connection_alert(
                            db.session, server_id_str, alert_engine.AlertSeverity.CRITICAL,
                            f"{hostname} ({ip_address}) unreachable: {exc}", "unreachable",
                            config=self.config, server=server,
                        )
                except (ObjectDeletedError, InvalidRequestError):
                    pass
                return

            alert_engine.resolve_connection_alerts(db.session, server_id_str, "auth_failed")
            alert_engine.resolve_connection_alerts(db.session, server_id_str, "unreachable")

        is_idrac7 = (topology.get("idrac_generation") == "idrac7")
        if topology.get("idrac_generation") == "idrac6":
            logger.info("iDRAC 6 detected for %s — branching to WS-Man polling", server.id)
            self._run_wsman_poll(server, password)
            return

        if topology.get("ilo_generation") in ("ilo2", "ilo3"):
            logger.info("HPE %s detected for %s — branching to RIBCL polling", topology["ilo_generation"].upper(), server.id)
            self._run_ribcl_poll(server, password)
            return

        collected_categories = set()

        # -- run every category collector ----------------------------------
        for category_name, collector_module in COLLECTOR_REGISTRY.items():
            logger.info("Polling started for %s on server %s", category_name, server.id)
            try:
                components, readings = collector_module.collect(client, server, topology)
                logger.info("Polling parsed successfully for %s (found %d components) on server %s", category_name, len(components), server.id)
                if len(components) > 0 or len(readings) > 0:
                    collected_categories.add(category_name)
            except Exception as exc:
                logger.exception("Parsing failed for collector '%s' on server %s: %s", category_name, server.id, exc)
                continue
            
            try:
                self._upsert_components(server, category_name, components)
                self._insert_readings(server, readings)
                alert_engine.evaluate_components(db.session, str(server.id), category_name, components, self.config, server=server)
                ws_events.emit_component_update(
                    self.socketio, str(server.id), category_name,
                    [self._component_dict(x) for x in components],
                )
                logger.info("Polling finished for %s - Stored %d components on server %s", category_name, len(components), server.id)
            except Exception as exc:
                db.session.rollback()
                logger.exception("Database insertion failed for collector '%s' on server %s (rolling back): %s", category_name, server.id, exc)
                continue

        # -- logs (append-only event stream) --------------------------------
        has_logs = False
        logger.info("Polling started for logs on server %s", server.id)
        try:
            log_dicts = logs_collector.collect(client, server, topology)
            if log_dicts:
                has_logs = True
            new_entries = self._upsert_logs(server, log_dicts)
            if new_entries:
                ws_events.emit_log_entries(self.socketio, str(server.id), new_entries)
            logger.info("Polling finished for logs - Stored %d entries on server %s", len(new_entries), server.id)
        except Exception as exc:
            db.session.rollback()
            logger.exception("Log collection failed for server %s (rolling back): %s", server.id, exc)

        # -- iDRAC 7 hybrid WS-Man fallback ----------------------------------
        if is_idrac7:
            from redfish.collectors.dell_wsman_collector import collect_wsman
            from redfish.dell_wsman import WsManClient
            
            missing_categories = []
            for c in ["processor", "memory", "storage", "network", "power", "thermal", "voltage", "fans", "firmware", "pcie_devices"]:
                if c not in collected_categories:
                    missing_categories.append(c)
            if not has_logs:
                missing_categories.append("logs")
                
            if missing_categories:
                logger.info("iDRAC 7 hybrid fallback: fetching missing categories via WS-Man: %s", missing_categories)
                ws_client = WsManClient(server.ip_address, server.username, password)
                try:
                    comp_dict, ws_readings, ws_logs = collect_wsman(ws_client, str(server.id), missing_categories)
                    
                    for cat_name, components in comp_dict.items():
                        if cat_name in missing_categories and components:
                            self._upsert_components(server, cat_name, components)
                            alert_engine.evaluate_components(db.session, str(server.id), cat_name, components, self.config)
                            ws_events.emit_component_update(
                                self.socketio, str(server.id), cat_name,
                                [self._component_dict(x) for x in components],
                            )
                    
                    if ws_readings:
                        self._insert_readings(server, ws_readings)
                        
                    if "logs" in missing_categories and ws_logs:
                        new_entries = self._upsert_logs(server, ws_logs)
                        if new_entries:
                            ws_events.emit_log_entries(self.socketio, str(server.id), new_entries)
                            
                except Exception as exc:
                    logger.exception("WS-Man fallback collection failed for %s: %s", server.id, exc)

        # -- iLO 4 hybrid RIBCL fallback ----------------------------------
        if topology.get("ilo_generation") == "ilo4":
            from redfish.hpe_ribcl import RibclClient
            from redfish.collectors import hpe_ribcl_collector
            
            missing_categories = []
            for c in ["processor", "memory", "fans", "thermal", "power"]:
                if c not in collected_categories:
                    missing_categories.append(c)
                    
            if missing_categories:
                logger.info("iLO 4 hybrid fallback: fetching missing categories via RIBCL: %s", missing_categories)
                ribcl_client = RibclClient(server.ip_address, server.username, password, self.config.get("VERIFY_TLS", False))
                try:
                    comp_dict, ribcl_readings = hpe_ribcl_collector.collect_ribcl(ribcl_client, str(server.id))
                    
                    for cat_name, components in comp_dict.items():
                        if cat_name in missing_categories and components:
                            self._upsert_components(server, cat_name, components)
                            alert_engine.evaluate_components(db.session, str(server.id), cat_name, components, self.config)
                            ws_events.emit_component_update(
                                self.socketio, str(server.id), cat_name,
                                [self._component_dict(x) for x in components],
                            )
                    
                    if ribcl_readings:
                        # Insert readings that match missing categories
                        filtered_readings = []
                        for r in ribcl_readings:
                            if (r["metric"] == "fan_speed" and "fans" in missing_categories) or \
                               (r["metric"] == "temperature" and "thermal" in missing_categories):
                                filtered_readings.append(r)
                        if filtered_readings:
                            self._insert_readings(server, filtered_readings)
                            
                except Exception as exc:
                    logger.exception("RIBCL fallback collection failed for %s: %s", server.id, exc)

        # -- event subscription (best-effort) --------------------------------
        webhook_base = self.config.get("PUBLIC_WEBHOOK_BASE_URL")
        if webhook_base and server.supports_event_service is not False:
            try:
                redfish_events.subscribe(
                    client, topology,
                    f"{webhook_base}/api/redfish/webhook",
                    server.id,
                )
            except Exception:
                logger.debug("Event subscription failed for %s (non-fatal)", server.id)

        # -- roll up health/connection/power --------------------------------
        self._recompute_server_summary(server)
        db.session.commit()
        ws_events.emit_server_summary_update(self.socketio, server.to_summary_dict())

    # -- helpers -----------------------------------------------------------

    def _run_wsman_poll(self, server, password):
        from redfish.dell_wsman import WsManClient
        from redfish.collectors import dell_wsman_collector

        client = WsManClient(server.ip_address, server.username, password)
        try:
            comp_dict, readings, logs = dell_wsman_collector.collect_wsman(client, str(server.id))
        except Exception as exc:
            logger.exception("WS-Man top-level collection failed for %s: %s", server.id, exc)
            self._mark_connection(server, ConnectionStatus.UNREACHABLE, f"WS-Man failed: {exc}")
            return

        for cat_name, components in comp_dict.items():
            self._upsert_components(server, cat_name, components)
            alert_engine.evaluate_components(db.session, str(server.id), cat_name, components, self.config)
            ws_events.emit_component_update(
                self.socketio, str(server.id), cat_name,
                [self._component_dict(x) for x in components],
            )

        if readings:
            self._insert_readings(server, readings)

        if logs:
            new_entries = self._upsert_logs(server, logs)
            if new_entries:
                ws_events.emit_log_entries(self.socketio, str(server.id), new_entries)

        self._recompute_server_summary(server)
        db.session.commit()
        ws_events.emit_server_summary_update(self.socketio, server.to_summary_dict())

    def _run_ribcl_poll(self, server, password):
        from redfish.hpe_ribcl import RibclClient
        from redfish.collectors import hpe_ribcl_collector

        client = RibclClient(server.ip_address, server.username, password, self.config.get("VERIFY_TLS", False))
        try:
            comp_dict, readings = hpe_ribcl_collector.collect_ribcl(client, str(server.id))
        except Exception as exc:
            logger.exception("RIBCL top-level collection failed for %s: %s", server.id, exc)
            self._mark_connection(server, ConnectionStatus.UNREACHABLE, f"RIBCL failed: {exc}")
            return

        for cat_name, components in comp_dict.items():
            self._upsert_components(server, cat_name, components)
            alert_engine.evaluate_components(db.session, str(server.id), cat_name, components, self.config)
            ws_events.emit_component_update(
                self.socketio, str(server.id), cat_name,
                [self._component_dict(x) for x in components],
            )

        if readings:
            self._insert_readings(server, readings)

        self._recompute_server_summary(server)
        db.session.commit()
        ws_events.emit_server_summary_update(self.socketio, server.to_summary_dict())

    def _get_topology(self, client, server):
        refresh_interval = self.config["INVENTORY_REFRESH_INTERVAL_SECONDS"]
        last_refresh = self._last_inventory_refresh.get(server.id)
        now = datetime.utcnow()
        if (
            server.id not in self._topology_cache
            or last_refresh is None
            or (now - last_refresh).total_seconds() > refresh_interval
        ):
            topology = inventory.refresh_inventory(client, server, db.session)
            self._topology_cache[server.id] = topology
            self._last_inventory_refresh[server.id] = now
        return self._topology_cache[server.id]

    def _upsert_components(self, server, category_name, components):
        collector_db_categories = {
            "storage": ["storage_controller", "storage_drive", "storage_volume", "storage_pool"],
            "enclosure": ["storage_enclosure"],
            "pcie_devices": ["pcie"],
            "logs": [],
        }
        db_categories = collector_db_categories.get(category_name, [category_name])
        
        collected_odata_ids = {c["odata_id"] for c in components}
        
        # Prune stale components that are no longer reported by the BMC
        for cat in db_categories:
            existing_components = Component.query.filter_by(server_id=server.id, category=cat).all()
            for existing in existing_components:
                if existing.odata_id not in collected_odata_ids:
                    db.session.delete(existing)

        for c in components:
            existing = Component.query.filter_by(
                server_id=server.id, category=c["category"], odata_id=c["odata_id"]
            ).first()
            if existing:
                existing.name = c["name"]
                existing.health = c["health"]
                existing.state = c["state"]
                existing.location = c["location"]
                existing.raw_json = c["raw_json"]
                existing.last_updated_at = datetime.utcnow()
                db.session.add(existing)
            else:
                db.session.add(Component(
                    server_id=server.id,
                    category=c["category"],
                    odata_id=c["odata_id"],
                    name=c["name"],
                    health=c["health"],
                    state=c["state"],
                    location=c["location"],
                    raw_json=c["raw_json"],
                ))
        db.session.commit()

    def _insert_readings(self, server, readings):
        now = datetime.utcnow()
        for r in readings:
            db.session.add(SensorReading(
                server_id=server.id,
                metric=r["metric"],
                source_name=r["source_name"],
                value=r["value"],
                unit=r.get("unit"),
                recorded_at=now,
            ))
        if readings:
            db.session.commit()

    def _upsert_logs(self, server, log_dicts):
        from dateutil import parser as dtparser
        new_entries = []
        for entry in log_dicts:
            exists = LogEntry.query.filter_by(
                server_id=server.id,
                log_service=entry["log_service"],
                entry_id=entry["entry_id"],
            ).first()
            if exists:
                continue
            created_at = datetime.utcnow()
            if entry.get("created_raw"):
                try:
                    created_at = dtparser.parse(entry["created_raw"]).replace(tzinfo=None)
                except (ValueError, TypeError):
                    pass
            db.session.add(LogEntry(
                server_id=server.id,
                log_service=entry["log_service"],
                entry_id=entry["entry_id"],
                severity=entry["severity"],
                message=entry["message"],
                message_id=entry["message_id"],
                sensor_type=entry["sensor_type"],
                created_at=created_at,
                raw_json=entry["raw_json"],
            ))
            new_entries.append(entry)
        if new_entries:
            db.session.commit()
        return new_entries

    def _mark_connection(self, server, status: ConnectionStatus, error_msg: str = "", server_id_str: str = None):
        from sqlalchemy.orm.exc import ObjectDeletedError
        from sqlalchemy.exc import InvalidRequestError

        if not server_id_str:
            try:
                server_id_str = str(getattr(server, "id", "unknown"))
            except (ObjectDeletedError, InvalidRequestError):
                server_id_str = "unknown"

        try:
            target_server = None
            if server_id_str != "unknown":
                target_server = db.session.get(Server, server_id_str)
            if not target_server:
                logger.warning("Server %s was deleted during poll, discarding connection status update.", server_id_str)
                return

            target_server.connection_status = status
            target_server.last_poll_error = error_msg
            db.session.add(target_server)
            db.session.commit()
            summary = target_server.to_summary_dict()
            ws_events.emit_server_summary_update(self.socketio, summary)
        except (ObjectDeletedError, InvalidRequestError):
            db.session.rollback()
            logger.warning("Server %s was deleted during poll, discarding connection status update.", server_id_str)
        except Exception as exc:
            db.session.rollback()
            logger.error("Failed to mark connection status for server %s: %s", server_id_str, exc)

    def _recompute_server_summary(self, server):
        server.connection_status = ConnectionStatus.CONNECTED
        server.last_successful_poll = datetime.utcnow()
        server.last_poll_error = None

        components = Component.query.filter_by(server_id=server.id).all()
        worst = HealthStatus.OK
        order = [HealthStatus.OK, HealthStatus.WARNING, HealthStatus.CRITICAL]

        for c in components:
            if not c.health:
                continue
            h = _normalize_health_status(c.health)
            if h and h in order:
                if order.index(h) > order.index(worst):
                    worst = h

        if not components:
            existing = _normalize_health_status(server.health_status)
            if existing:
                worst = existing

        server.health_status = worst
        db.session.add(server)

    def _prune_old_readings(self):
        with self.app.app_context():
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=self.config["SENSOR_HISTORY_RETENTION_DAYS"])
            deleted = SensorReading.query.filter(SensorReading.recorded_at < cutoff).delete()
            db.session.commit()
            if deleted:
                logger.info("Pruned %d sensor readings older than %d days",
                            deleted, self.config["SENSOR_HISTORY_RETENTION_DAYS"])

    @staticmethod
    def _component_dict(c):
        return {
            "category": c["category"].value if hasattr(c["category"], "value") else c["category"],
            "odata_id": c["odata_id"],
            "name": c["name"],
            "health": c["health"],
            "state": c["state"],
            "location": c["location"],
            "properties": c["raw_json"],
        }