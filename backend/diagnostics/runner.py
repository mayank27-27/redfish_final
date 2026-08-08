"""
diagnostics/runner.py
========================
Bridges the DiagnosticsAdapter interface (synchronous, blocking, with a
progress_cb) to the Operation tracking layer. This function runs on a
background worker thread, not the Flask request thread.

Client construction deliberately reuses the same process-wide
PollingEngine singleton (session_manager + config) that the scheduler
uses for regular polling, rather than creating a second, independent
session-management path - see scheduler/poller.py's
_set_engine_instance()/_polling_engine_instance pattern.
"""
import logging
import os

from flask import Flask

from . import get_adapter
from .exceptions import DiagnosticsError, DiagnosticsUnsupported
from operations import service as ops

logger = logging.getLogger(__name__)

BUNDLE_STORAGE_DIR = os.environ.get("DIAGNOSTICS_STORAGE_DIR", "./instance/diagnostics_bundles")


def _build_client_for(server):
    """Mirrors exactly how scheduler/poller.py builds a client for a
    server, reusing the same process-wide PollingEngine singleton so
    diagnostics jobs share session state with regular polling instead
    of authenticating a second, independent session."""
    from scheduler.poller import _polling_engine_instance
    from auth.credentials import get_cipher
    from redfish.client import RedfishClient

    engine = _polling_engine_instance
    if engine is None:
        raise DiagnosticsError(
            "Polling engine not initialized - cannot build a Redfish client for diagnostics"
        )

    cipher = get_cipher(engine.config)
    password = cipher.decrypt(server.password_encrypted) if server.password_encrypted else ""
    base_url = f"https://{server.ip_address}"

    redfish_session = engine.session_manager.get_session(
        server.id, base_url, server.username, password,
    )
    return RedfishClient(redfish_session, engine.config)


def run_diagnostics_operation(app: Flask, operation_id: int, server_id: int):
    with app.app_context():
        from database.models import Server  # local import to avoid circulars

        server = Server.query.get(server_id)
        if not server:
            ops.mark_failed(operation_id, f"Server {server_id} no longer exists")
            return

        ops.mark_running(operation_id)

        def progress_cb(percent):
            ops.update_progress(operation_id, percent)

        try:
            adapter = get_adapter(server.vendor)
            client = _build_client_for(server)
            result = adapter.download_support_bundle(client, server, progress_cb=progress_cb)

            os.makedirs(BUNDLE_STORAGE_DIR, exist_ok=True)
            dest_path = os.path.join(BUNDLE_STORAGE_DIR, f"op_{operation_id}_{result.filename}")
            with open(dest_path, "wb") as f:
                f.write(result.content)

            ops.mark_completed(
                operation_id,
                result_path=dest_path,
                result_filename=result.filename,
                result_content_type=result.content_type,
            )
        except DiagnosticsUnsupported as e:
            ops.mark_failed(operation_id, str(e))
        except DiagnosticsError as e:
            ops.mark_failed(operation_id, str(e))
        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected error in diagnostics operation %s", operation_id)
            ops.mark_failed(operation_id, f"Unexpected error: {e}")