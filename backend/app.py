"""
app.py
=======
Flask application factory + all REST API routes + SocketIO setup.

STARTUP ORDER (critical):
  1. python-dotenv loads .env into os.environ (very first thing in __main__)
  2. build_app_config() reads os.environ and returns an AppConfig object
  3. AppConfig is stored in Flask's app.config AND passed to submodules
  4. db.init_app(app), SocketIO, routes are registered
  5. PollingEngine is created (receives app + socketio)
  6. socketio.run() starts the server

The browser talks ONLY to these REST endpoints and WebSocket; it never
reaches Redfish/BMC endpoints directly.

REST API
--------
GET  /api/servers                          list all servers
POST /api/servers                          add server
GET  /api/servers/<id>                     server detail
PATCH /api/servers/<id>                    update settings
DELETE /api/servers/<id>                   remove server
POST /api/servers/<id>/poll-now            immediate poll
GET  /api/servers/<id>/components          hardware state grouped by category
GET  /api/servers/<id>/history/<metric>    time-series (range=1h/24h/7d/30d)
GET  /api/servers/<id>/logs                event logs
POST /api/servers/<id>/diagnostics/support-bundle
GET  /api/operations/<id>                  operation status
GET  /api/operations/<id>/download         completed operation result
GET  /api/alerts                           alert list
POST /api/alerts/<id>/acknowledge
POST /api/alerts/<id>/resolve
POST /api/redfish/webhook                  Redfish EventService inbound
GET  /api/health                           health check
GET  /api/storage-devices                  list storage arrays (MSA etc)
POST /api/storage-devices                  add storage array
GET  /api/storage-devices/<id>             storage array detail
DELETE /api/storage-devices/<id>           remove storage array
GET  /api/storage-devices/<id>/components  hardware state grouped by category
POST /api/storage-devices/<id>/poll-now    immediate poll (not yet implemented)
"""
# ── CRITICAL: load .env into os.environ BEFORE anything reads env vars ──────
# This must be the FIRST executable code in the file.
from dotenv import load_dotenv
load_dotenv()  # reads backend/.env (or wherever the process cwd is)

import logging
import os
import hashlib
import secrets
from collections import defaultdict
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, abort, render_template, send_file
from flask_socketio import SocketIO

from config import build_app_config
from database import db
from database.models import (
    Server, Component, SensorReading, LogEntry, Alert,
    ConnectionStatus, Agent, Site, StorageDevice, StorageComponent
)
from auth.credentials import get_cipher
from websocket import events as ws_events
from redfish.events import parse_event_payload
from alerts import engine as alert_engine
from diagnostics.runner import run_diagnostics_operation
from operations import executor as operation_executor
from operations import service as operation_service

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Application factory
# ─────────────────────────────────────────────────────────────────────────────

def create_app() -> tuple[Flask, SocketIO]:
    """Create and return (app, socketio).

    The polling engine is started separately by the __main__ block so that
    tests can import create_app without triggering background threads.
    """
    # Build our unified config object (reads os.environ, validates, logs summary)
    cfg = build_app_config()

    app = Flask(__name__, template_folder="templates", static_folder="static")

    # Push ALL config values into Flask's app.config so Flask extensions
    # (SQLAlchemy, etc.) can find their expected keys.
    app.config.update(
        SECRET_KEY=cfg.SECRET_KEY,
        DEBUG=cfg.DEBUG,
        SQLALCHEMY_DATABASE_URI=cfg.SQLALCHEMY_DATABASE_URI,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS=cfg.SQLALCHEMY_ENGINE_OPTIONS,
        ENCRYPTION_KEY=cfg.ENCRYPTION_KEY,
        REDFISH_VERIFY_TLS=cfg.REDFISH_VERIFY_TLS,
        REDFISH_HTTP_TIMEOUT=cfg.REDFISH_HTTP_TIMEOUT,
        REDFISH_MAX_RETRIES=cfg.REDFISH_MAX_RETRIES,
        REDFISH_RETRY_BACKOFF_SECONDS=cfg.REDFISH_RETRY_BACKOFF_SECONDS,
        REDFISH_SESSION_REFRESH_MARGIN=cfg.REDFISH_SESSION_REFRESH_MARGIN,
        DEFAULT_POLLING_INTERVAL_SECONDS=cfg.DEFAULT_POLLING_INTERVAL_SECONDS,
        MAX_CONCURRENT_POLLS=cfg.MAX_CONCURRENT_POLLS,
        INVENTORY_REFRESH_INTERVAL_SECONDS=cfg.INVENTORY_REFRESH_INTERVAL_SECONDS,
        FALLBACK_TEMPERATURE_CRITICAL_C=cfg.FALLBACK_TEMPERATURE_CRITICAL_C,
        SENSOR_HISTORY_RETENTION_DAYS=cfg.SENSOR_HISTORY_RETENTION_DAYS,
        PUBLIC_WEBHOOK_BASE_URL=cfg.PUBLIC_WEBHOOK_BASE_URL,
        SOCKETIO_ASYNC_MODE=cfg.SOCKETIO_ASYNC_MODE,
        SOCKETIO_MESSAGE_QUEUE=cfg.SOCKETIO_MESSAGE_QUEUE,
        CORS_ALLOWED_ORIGINS=cfg.CORS_ALLOWED_ORIGINS,
    )

    # Store the full AppConfig object so submodules that need attribute-style
    # access (session.py, client.py) can retrieve it as app.config['REDFISH_CONFIG']
    app.config['REDFISH_CONFIG'] = cfg

    # SQLAlchemy
    db.init_app(app)

    # SocketIO
    socketio = SocketIO(
        app,
        cors_allowed_origins=cfg.CORS_ALLOWED_ORIGINS,
        async_mode=cfg.SOCKETIO_ASYNC_MODE,
        message_queue=cfg.SOCKETIO_MESSAGE_QUEUE,
        logger=cfg.DEBUG,
        engineio_logger=False,
    )

    # Register WebSocket handlers
    ws_events.register_handlers(socketio)

    # Register REST routes
    _register_routes(app, socketio)

    # Create DB tables
    with app.app_context():
        db.create_all()
        operation_service.reconcile_orphaned_operations()

    return app, socketio


# ─────────────────────────────────────────────────────────────────────────────
# Route registration
# ─────────────────────────────────────────────────────────────────────────────

def _register_routes(app: Flask, socketio: SocketIO):

    @app.route("/")
    def index():
        return render_template("index.html")

    # ── Servers ──────────────────────────────────────────────────────────
    @app.route("/api/servers", methods=["GET"])
    def list_servers():
        servers = Server.query.order_by(Server.hostname).all()
        return jsonify([s.to_summary_dict() for s in servers])

    @app.route("/api/servers", methods=["POST"])
    def add_server():
        data = request.get_json(force=True) or {}
        for field in ("hostname", "ip_address"):
            if not data.get(field):
                return jsonify({"error": f"'{field}' is required"}), 400
        
        # Credentials are required ONLY if not managed by an agent
        agent_id = data.get("agent_id")
        if not agent_id:
            agent_id = None
            for field in ("username", "password"):
                if not data.get(field):
                    return jsonify({"error": f"'{field}' is required for local polling"}), 400
        else:
            try:
                import uuid
                uuid.UUID(agent_id)
            except ValueError:
                return jsonify({"error": "Agent ID must be a valid UUID format"}), 400
            if not db.session.get(Agent, agent_id):
                return jsonify({"error": "Agent ID does not exist in the database"}), 400

        site_id = data.get("site_id")
        if not site_id:
            site_id = None
        else:
            try:
                import uuid
                uuid.UUID(site_id)
            except ValueError:
                return jsonify({"error": "Site ID must be a valid UUID format"}), 400
            if not db.session.get(Site, site_id):
                return jsonify({"error": "Site ID does not exist in the database"}), 400

        if Server.query.filter_by(ip_address=data["ip_address"]).first():
            return jsonify({"error": "A server with this IP address already exists"}), 409

        management_protocol = data.get("management_protocol", "redfish")
        if management_protocol not in ["redfish", "ilo2"]:
            return jsonify({"error": "Invalid management protocol"}), 400

        device_type = data.get("device_type", "server")
        if device_type not in ["server", "storage"]:
            return jsonify({"error": "Invalid device_type (must be 'server' or 'storage')"}), 400

        cipher = get_cipher(app.config)
        server = Server(
            hostname=data["hostname"],
            display_name=data.get("display_name") or data["hostname"],
            ip_address=data["ip_address"],
            username=data.get("username"),
            password_encrypted=cipher.encrypt(data["password"]) if data.get("password") else None,
            polling_interval_seconds=int(data.get("polling_interval_seconds") or 30),
            enabled=True,
            site_id=site_id,
            agent_id=agent_id,
            customer_name=data.get("customer_name"),
            customer_location=data.get("customer_location"),
            maintenance_records=data.get("maintenance_records"),
            management_protocol=management_protocol,
            device_type=device_type,
        )
        db.session.add(server)
        db.session.commit()
        _trigger_poll(server.id)
        return jsonify(server.to_dict()), 201

    @app.route("/api/servers/<server_id>", methods=["GET"])
    def get_server(server_id):
        return jsonify(_get_server_or_404(server_id).to_dict())

    @app.route("/api/servers/<server_id>", methods=["PATCH"])
    def update_server(server_id):
        server = _get_server_or_404(server_id)
        data   = request.get_json(force=True) or {}
        cipher = get_cipher(app.config)
        if "hostname" in data:              server.hostname = data["hostname"]
        if "display_name" in data:          server.display_name = data["display_name"]
        if "ip_address" in data:            server.ip_address = data["ip_address"]
        if "username" in data:              server.username = data["username"]
        if "password" in data:              server.password_encrypted = cipher.encrypt(data["password"]) if data["password"] else None
        if "customer_name" in data:         server.customer_name = data["customer_name"]
        if "customer_location" in data:     server.customer_location = data["customer_location"]
        if "maintenance_records" in data:   server.maintenance_records = data["maintenance_records"]
        if "device_type" in data:
            if data["device_type"] in ["server", "storage"]:
                server.device_type = data["device_type"]
            else:
                return jsonify({"error": "Invalid device_type"}), 400
        if "management_protocol" in data:
            val = data["management_protocol"]
            if val not in ["redfish", "ilo2"]:
                return jsonify({"error": "Invalid management protocol"}), 400
            server.management_protocol = val
        if "site_id" in data:
            if data["site_id"]:
                try:
                    import uuid
                    uuid.UUID(data["site_id"])
                    if not db.session.get(Site, data["site_id"]):
                        return jsonify({"error": "Site ID does not exist in the database"}), 400
                    server.site_id = data["site_id"]
                except ValueError:
                    return jsonify({"error": "Site ID must be a valid UUID format"}), 400
            else:
                server.site_id = None
                
        if "agent_id" in data:
            if data["agent_id"]:
                try:
                    import uuid
                    uuid.UUID(data["agent_id"])
                    if not db.session.get(Agent, data["agent_id"]):
                        return jsonify({"error": "Agent ID does not exist in the database"}), 400
                    server.agent_id = data["agent_id"]
                except ValueError:
                    return jsonify({"error": "Agent ID must be a valid UUID format"}), 400
            else:
                server.agent_id = None
                
        if "polling_interval_seconds" in data:
            server.polling_interval_seconds = int(data["polling_interval_seconds"])
        if "enabled" in data:               server.enabled = bool(data["enabled"])
        db.session.commit()
        return jsonify(server.to_dict())

    @app.route("/api/servers/<server_id>", methods=["DELETE"])
    def delete_server(server_id):
        server = _get_server_or_404(server_id)
        server_db_id = server.id
        db.session.delete(server)
        db.session.commit()
        try:
            from scheduler.poller import _polling_engine_instance
            if _polling_engine_instance:
                _polling_engine_instance.session_manager.drop_session(server_db_id)
        except Exception:
            pass
        return "", 204

    @app.route("/api/servers/<server_id>/poll-now", methods=["POST"])
    def poll_now(server_id):
        _get_server_or_404(server_id)
        _trigger_poll(server_id)
        return jsonify({"status": "queued"})

    # ── Diagnostics operations ──────────────────────────────────────────
    @app.route("/api/servers/<server_id>/diagnostics/support-bundle", methods=["POST"])
    def start_support_bundle(server_id):
        server = _get_server_or_404(server_id)
        if server.agent_id is not None:
            return jsonify({"error": "Diagnostics operations are not supported for agent-managed servers"}), 400
        try:
            operation = operation_service.create_operation(
                server.id, "support_bundle", server.vendor,
            )
        except operation_service.OperationConflict as exc:
            return jsonify({
                "error": str(exc),
                "operation": exc.existing_operation.to_dict(),
            }), 409

        operation_executor.submit(
            run_diagnostics_operation, app, operation.id, server.id,
        )
        return jsonify(operation.to_dict()), 202

    @app.route("/api/operations/<int:operation_id>", methods=["GET"])
    def get_operation(operation_id):
        operation = operation_service.get_operation(operation_id)
        if not operation:
            abort(404, description=f"Operation {operation_id} not found")
        return jsonify(operation.to_dict())

    @app.route("/api/operations/<int:operation_id>/download", methods=["GET"])
    def download_operation_result(operation_id):
        operation = operation_service.get_operation(operation_id)
        if not operation:
            abort(404, description=f"Operation {operation_id} not found")
        if not operation.result_path or not os.path.isfile(operation.result_path):
            abort(404, description="Operation result is not available")
        return send_file(
            operation.result_path,
            as_attachment=True,
            download_name=operation.result_filename,
            mimetype=operation.result_content_type,
        )

    # ── Components ────────────────────────────────────────────────────────
    @app.route("/api/servers/<server_id>/components", methods=["GET"])
    def get_components(server_id):
        _get_server_or_404(server_id)
        category_filter = request.args.get("category")
        q = Component.query.filter_by(server_id=server_id)
        if category_filter:
            q = q.filter_by(category=category_filter)
        grouped: dict[str, list] = defaultdict(list)
        for c in q.order_by(Component.category, Component.name).all():
            grouped[c.category].append(c.to_dict())
        return jsonify(grouped)

    # ── Sensor history ────────────────────────────────────────────────────
    @app.route("/api/servers/<server_id>/history/<metric>", methods=["GET"])
    def get_history(server_id, metric):
        _get_server_or_404(server_id)
        hours = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}.get(request.args.get("range", "1h"), 1)
        since = datetime.utcnow() - timedelta(hours=hours)
        rows = (
            SensorReading.query
            .filter_by(server_id=server_id, metric=metric)
            .filter(SensorReading.recorded_at >= since)
            .order_by(SensorReading.recorded_at)
            .all()
        )
        return jsonify([r.to_dict() for r in rows])

    # ── Logs ──────────────────────────────────────────────────────────────
    @app.route("/api/servers/<server_id>/logs", methods=["GET"])
    def get_logs(server_id):
        _get_server_or_404(server_id)
        q = LogEntry.query.filter_by(server_id=server_id)
        if s := request.args.get("q", "").strip():   q = q.filter(LogEntry.message.ilike(f"%{s}%"))
        if s := request.args.get("severity", ""):     q = q.filter_by(severity=s)
        if s := request.args.get("log_service", ""):  q = q.filter_by(log_service=s)
        limit = min(int(request.args.get("limit", 500)), 2000)
        rows = q.order_by(LogEntry.created_at.desc()).limit(limit).all()
        return jsonify([r.to_dict() for r in rows])

    # ── Alerts ────────────────────────────────────────────────────────────
    @app.route("/api/alerts", methods=["GET"])
    def list_alerts():
        show_resolved = request.args.get("resolved", "false").lower() == "true"
        q = Alert.query
        if not show_resolved:
            q = q.filter_by(resolved=False)
        if sid := request.args.get("server_id"):
            q = q.filter_by(server_id=sid)
        return jsonify([a.to_dict() for a in q.order_by(Alert.last_occurred_at.desc()).limit(500)])

    @app.route("/api/alerts/<alert_id>/acknowledge", methods=["POST"])
    def acknowledge_alert(alert_id):
        alert = db.session.get(Alert, alert_id) or abort(404)
        alert.acknowledged = True
        alert.acknowledged_at = datetime.utcnow()
        db.session.commit()
        return jsonify(alert.to_dict())

    @app.route("/api/alerts/<alert_id>/resolve", methods=["POST"])
    def resolve_alert(alert_id):
        alert = db.session.get(Alert, alert_id) or abort(404)
        alert.resolved = True
        alert.resolved_at = datetime.utcnow()
        db.session.commit()
        return jsonify(alert.to_dict())

    # ── Redfish EventService webhook ──────────────────────────────────────
    @app.route("/api/redfish/webhook", methods=["POST"])
    def redfish_webhook():
        data = request.get_json(force=True, silent=True) or {}
        events = parse_event_payload(data)
        server_id = data.get("Context")
        if not server_id or not events:
            return "", 204
        server = db.session.get(Server, server_id)
        if not server:
            return "", 204
        for ev in events:
            sev_str = ev.get("severity", "OK").lower()
            if sev_str not in ("critical", "warning"):
                continue
            sev = alert_engine.AlertSeverity.CRITICAL if sev_str == "critical" else alert_engine.AlertSeverity.WARNING
            alert_engine._raise_or_bump(
                db.session, server_id, "event", sev,
                ev.get("message") or ev.get("message_id") or "Redfish event",
                ev.get("origin"), ev.get("message_id") or "webhook_event",
            )
        db.session.commit()
        ws_events.emit_alert(socketio, server_id, {})
        _trigger_poll(server_id)
        return "", 204

    # ── Health check ──────────────────────────────────────────────────────
    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


    # ── Agent Enrollment ─────────────────────────────────────────────────
    @app.route("/api/agents/enroll", methods=["POST"])
    def enroll_agent():
        """Self-registration endpoint for remote agents.

        The agent sends an enrollment_key (shared secret from the company),
        its name, customer details, and list of BMC devices. The server
        creates the Agent + Site + Server records and returns a unique
        bearer token + server_id mappings so the agent can start pushing
        telemetry immediately.
        """
        enrollment_key_hash = app.config.get("ENROLLMENT_KEY_HASH") or app.config["REDFISH_CONFIG"].get("ENROLLMENT_KEY_HASH", "")
        if not enrollment_key_hash:
            return jsonify({"error": "Agent enrollment is not configured on this server"}), 503

        data = request.get_json(force=True) or {}
        enrollment_key = data.get("enrollment_key", "")
        if not enrollment_key:
            return jsonify({"error": "enrollment_key is required"}), 400

        # Verify enrollment key
        provided_hash = hashlib.sha256(enrollment_key.encode()).hexdigest()
        if provided_hash != enrollment_key_hash:
            return jsonify({"error": "Invalid enrollment key"}), 403

        agent_name = data.get("agent_name", "").strip()
        if not agent_name:
            return jsonify({"error": "agent_name is required"}), 400

        # Check for duplicate agent name
        if Agent.query.filter_by(name=agent_name).first():
            return jsonify({"error": f"Agent '{agent_name}' already exists"}), 409

        customer_name = data.get("customer_name", "").strip()
        customer_location = data.get("customer_location", "").strip()
        devices = data.get("devices", [])

        if not devices:
            return jsonify({"error": "At least one device is required in 'devices'"}), 400

        # Create Site (if customer info provided)
        site = None
        site_name = customer_name or agent_name
        existing_site = Site.query.filter_by(name=site_name).first()
        if existing_site:
            site = existing_site
        else:
            site = Site(
                name=site_name,
                description=f"{customer_location}" if customer_location else None,
            )
            db.session.add(site)
            db.session.flush()  # get site.id

        # Generate agent token (same mechanism as create_agent.py)
        raw_token = secrets.token_hex(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        agent = Agent(
            name=agent_name,
            api_key_hash=token_hash,
            site_id=site.id,
            health_status="Enrolled",
        )
        db.session.add(agent)
        db.session.flush()  # get agent.id

        # Create Server records for each device
        created_devices = []
        cipher = get_cipher(app.config)
        for dev in devices:
            ip = dev.get("ip_address", "").strip()
            if not ip:
                continue

            # Skip if server with this IP already exists
            existing = Server.query.filter_by(ip_address=ip).first()
            if existing:
                # Re-assign to this agent if not already assigned
                if not existing.agent_id:
                    existing.agent_id = agent.id
                    existing.site_id = site.id
                    existing.customer_name = customer_name or existing.customer_name
                    existing.customer_location = customer_location or existing.customer_location
                created_devices.append({
                    "server_id": str(existing.id),
                    "ip_address": ip,
                    "hostname": existing.hostname,
                    "status": "existing",
                })
                continue

            hostname = dev.get("hostname", "").strip() or ip
            server = Server(
                hostname=hostname,
                display_name=hostname,
                ip_address=ip,
                username=dev.get("username"),
                password_encrypted=cipher.encrypt(dev["password"]) if dev.get("password") else None,
                polling_interval_seconds=int(dev.get("polling_interval_seconds", 30)),
                enabled=True,
                site_id=site.id,
                agent_id=agent.id,
                customer_name=customer_name,
                customer_location=customer_location,
            )
            db.session.add(server)
            db.session.flush()
            created_devices.append({
                "server_id": str(server.id),
                "ip_address": ip,
                "hostname": hostname,
                "status": "created",
            })

        db.session.commit()
        logger.info("Agent '%s' enrolled successfully with %d device(s)", agent_name, len(created_devices))

        return jsonify({
            "agent_id": str(agent.id),
            "agent_token": raw_token,
            "site_id": str(site.id),
            "devices": created_devices,
        }), 201

    # ── Agent Heartbeat ──────────────────────────────────────────────────
    @app.route("/api/agents/heartbeat", methods=["POST"])
    def agent_heartbeat():
        """Lightweight liveness endpoint for agents.

        Agents call this periodically so the Central Server can track
        online/offline status even when there is no telemetry to push.
        """
        agent_token = request.headers.get("X-Agent-Token")
        if not agent_token:
            return jsonify({"error": "Missing X-Agent-Token"}), 401

        token_hash = hashlib.sha256(agent_token.encode()).hexdigest()
        agent = Agent.query.filter_by(api_key_hash=token_hash).first()
        if not agent:
            return jsonify({"error": "Invalid X-Agent-Token"}), 403

        agent.last_seen_at = datetime.utcnow()
        agent.health_status = "OK"
        db.session.commit()

        return jsonify({"status": "ok", "agent_id": str(agent.id)}), 200

    # ── Agent Ingestion ───────────────────────────────────────────────────
    @app.route("/api/ingest/telemetry", methods=["POST"])
    def ingest_telemetry():
        agent_token = request.headers.get("X-Agent-Token")
        if not agent_token:
            return jsonify({"error": "Missing X-Agent-Token"}), 401
        
        from database.models import ConnectionStatus
        token_hash = hashlib.sha256(agent_token.encode()).hexdigest()
        agent = Agent.query.filter_by(api_key_hash=token_hash).first()
        if not agent:
            return jsonify({"error": "Invalid X-Agent-Token"}), 403

        data = request.get_json(force=True) or {}
        server_id = data.get("server_id")
        if not server_id:
            return jsonify({"error": "server_id is required"}), 400
            
        server = _get_server_or_404(server_id)
        if str(server.agent_id) != str(agent.id):
            return jsonify({"error": "Server not assigned to this agent"}), 403

        # Update agent last seen
        agent.last_seen_at = datetime.utcnow()
        agent.health_status = "OK"
        db.session.add(agent)

        components = data.get("components", {})
        readings = data.get("readings", [])
        logs = data.get("logs", [])
        connection_status = data.get("connection_status")
        connection_error = data.get("connection_error")
        inventory = data.get("inventory")

        from scheduler.poller import _polling_engine_instance
        engine = _polling_engine_instance
        if not engine:
            return jsonify({"error": "Polling engine not running"}), 503

        if inventory:
            for k, v in inventory.items():
                if hasattr(server, k):
                    setattr(server, k, v)

        if connection_status:
            try:
                status_enum = ConnectionStatus(connection_status)
                engine._mark_connection(server, status_enum, connection_error or "")
            except ValueError:
                pass

        if components:
            for cat, comp_list in components.items():
                engine._upsert_components(server, cat, comp_list)
                alert_engine.evaluate_components(db.session, str(server.id), cat, comp_list, app.config["REDFISH_CONFIG"], server=server)
                ws_events.emit_component_update(socketio, str(server.id), cat, [engine._component_dict(x) for x in comp_list])

        if readings:
            engine._insert_readings(server, readings)

        if logs:
            new_entries = engine._upsert_logs(server, logs)
            if new_entries:
                ws_events.emit_log_entries(socketio, str(server.id), new_entries)

        if connection_status == "connected":
            engine._recompute_server_summary(server)
        else:
            # Re-read from DB to get the status we just set in _mark_connection
            pass
        db.session.commit()
        ws_events.emit_server_summary_update(socketio, server.to_summary_dict())

        return jsonify({"status": "ok"}), 200

    # ── Storage Devices (standalone arrays - HPE MSA etc, not server BMCs) ──
    @app.route("/api/storage-devices", methods=["GET"])
    def list_storage_devices():
        devices = StorageDevice.query.order_by(StorageDevice.hostname).all()
        return jsonify([d.to_dict() for d in devices])

    @app.route("/api/storage-devices", methods=["POST"])
    def add_storage_device():
        data = request.get_json(force=True) or {}
        for field in ("hostname", "ip_address", "username", "password"):
            if not data.get(field):
                return jsonify({"error": f"'{field}' is required"}), 400

        if StorageDevice.query.filter_by(ip_address=data["ip_address"]).first():
            return jsonify({"error": "A storage device with this IP address already exists"}), 409

        cipher = get_cipher(app.config)
        device = StorageDevice(
            hostname=data["hostname"],
            display_name=data.get("display_name") or data["hostname"],
            ip_address=data["ip_address"],
            username=data.get("username"),
            password_encrypted=cipher.encrypt(data["password"]) if data.get("password") else None,
            polling_interval_seconds=int(data.get("polling_interval_seconds") or 30),
            enabled=True,
        )
        db.session.add(device)
        db.session.commit()
        return jsonify(device.to_dict()), 201
    
    @app.route("/api/storage-devices/<device_id>", methods=["PATCH"])
    def update_storage_device(device_id):
        device = db.session.get(StorageDevice, device_id)
        if not device:
            abort(404, description=f"Storage device {device_id} not found")
        data = request.get_json(force=True) or {}
        cipher = get_cipher(app.config)
        if "hostname" in data:           device.hostname = data["hostname"]
        if "display_name" in data:       device.display_name = data["display_name"]
        if "ip_address" in data:         device.ip_address = data["ip_address"]
        if "username" in data:           device.username = data["username"]
        if "password" in data:           device.password_encrypted = cipher.encrypt(data["password"]) if data["password"] else None
        if "polling_interval_seconds" in data:
            device.polling_interval_seconds = int(data["polling_interval_seconds"])
        if "enabled" in data:            device.enabled = bool(data["enabled"])
        db.session.commit()
        return jsonify(device.to_dict())

    @app.route("/api/storage-devices/<device_id>", methods=["GET"])
    def get_storage_device(device_id):
        device = db.session.get(StorageDevice, device_id)
        if not device:
            abort(404, description=f"Storage device {device_id} not found")
        return jsonify(device.to_dict())

    @app.route("/api/storage-devices/<device_id>", methods=["DELETE"])
    def delete_storage_device(device_id):
        device = db.session.get(StorageDevice, device_id)
        if not device:
            abort(404, description=f"Storage device {device_id} not found")
        db.session.delete(device)
        db.session.commit()
        return "", 204

    @app.route("/api/storage-devices/<device_id>/components", methods=["GET"])
    def get_storage_device_components(device_id):
        device = db.session.get(StorageDevice, device_id)
        if not device:
            abort(404, description=f"Storage device {device_id} not found")
        comps = StorageComponent.query.filter_by(storage_device_id=device_id).all()
        grouped: dict[str, list] = defaultdict(list)
        for c in comps:
            grouped[c.category].append({
                "odata_id": c.odata_id, "name": c.name, "health": c.health,
                "state": c.state, "location": c.location, "properties": c.raw_json,
            })
        return jsonify(grouped)

    @app.route("/api/storage-devices/<device_id>/poll-now", methods=["POST"])
    def poll_storage_device_now(device_id):
        device = db.session.get(StorageDevice, device_id)
        if not device:
            abort(404, description=f"Storage device {device_id} not found")
        from storage_devices.storage_poller import _storage_engine_instance
        if _storage_engine_instance:
            _storage_engine_instance.poll_one(device_id)
        return jsonify({"status": "queued"})

    @app.route("/api/servers/<server_id>/diagnostics/redfish-tree", methods=["GET"])
    def get_redfish_tree(server_id):
        server = _get_server_or_404(server_id)
        cipher = get_cipher(app.config)
        try:
            password = cipher.decrypt(server.password_encrypted) if server.password_encrypted else None
        except Exception:
            return jsonify({"error": "Failed to decrypt stored credentials"}), 500

        from scheduler.poller import _polling_engine_instance
        from redfish.client import RedfishClient
        from diagnostics.redfish_explorer import explore_redfish
        base_url = f"https://{server.ip_address}"
        try:
            if _polling_engine_instance and getattr(_polling_engine_instance, "session_manager", None):
                session = _polling_engine_instance.session_manager.get_session(server.id, base_url, server.username or "", password or "")
                cfg = _polling_engine_instance.config
            else:
                from redfish.session import RedfishSessionManager
                cfg = app.config.get("REDFISH_CONFIG")
                session_mgr = RedfishSessionManager(cfg)
                session = session_mgr.get_session(server.id, base_url, server.username or "", password or "")
            client = RedfishClient(session, cfg)
            tree = explore_redfish(client)
            return jsonify(tree)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 502


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_server_or_404(server_id: str) -> Server:
    server = db.session.get(Server, server_id)
    if not server:
        abort(404, description=f"Server {server_id} not found")
    return server


def _trigger_poll(server_id):
    try:
        from scheduler.poller import _polling_engine_instance
        if _polling_engine_instance:
            _polling_engine_instance.poll_server_now(str(server_id))
    except (ImportError, AttributeError):
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # NOTE: load_dotenv() was already called at the top of this file.
    # monkey_patch must happen before any other eventlet-aware code.
    import eventlet
    eventlet.monkey_patch()

    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DEBUG", "false").lower() == "true" else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app, socketio = create_app()

    # Start polling engine using the AppConfig stored inside app.config
    from scheduler.poller import PollingEngine, _set_engine_instance
    redfish_cfg = app.config["REDFISH_CONFIG"]
    engine = PollingEngine(app, socketio, redfish_cfg)
    _set_engine_instance(engine)
    engine.start()

    from storage_devices.storage_poller import StoragePollingEngine, _set_storage_engine_instance
    storage_engine = StoragePollingEngine(app, redfish_cfg)
    _set_storage_engine_instance(storage_engine)
    storage_engine.start()

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    logger.info("Listening on http://%s:%d", host, port)
    socketio.run(app, host=host, port=port, debug=app.config.get("DEBUG", False))

