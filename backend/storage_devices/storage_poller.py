"""
storage_devices/storage_poller.py
===================================
Background polling engine for standalone storage arrays (HPE MSA etc).

Deliberately separate from scheduler/poller.py's PollingEngine (which
handles Server/BMC polling) rather than extending it - storage devices
have a completely different auth model (MSAClient, not Redfish sessions)
and a different DB shape (StorageDevice/StorageComponent, not
Server/Component). Keeping them separate avoids any risk of an edit
here breaking the working server-polling pipeline.

Each poll cycle, for every enabled StorageDevice whose interval has
elapsed:
  1. Decrypt its stored password, build an MSAClient, log in.
  2. Collect components + readings via storage_devices.msa_collector.
  3. On success: replace all StorageComponent rows for this device
     (delete-then-insert, so a component that disappears - e.g. a
     drive pulled - doesn't linger forever), insert new
     StorageSensorReading rows, update connection/health/timestamps.
  4. On failure: record the error on the device row so the UI can
     show it, without touching existing component data (so the last-
     known state stays visible instead of vanishing on a blip).

NOTE: this does not yet push WebSocket updates - the frontend picks up
fresh data on next page load / re-navigation into the device, not
instantly. Live-push can be added later the same way server polling
does it, once this basic loop is confirmed working.
"""
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from database import db
from database.models import StorageDevice, StorageComponent, StorageSensorReading, ConnectionStatus
from auth.credentials import get_cipher
from .msa_client import MSAClient, MSAAuthError, MSAUnreachableError
from .msa_collector import collect

logger = logging.getLogger(__name__)

_storage_engine_instance = None


def _set_storage_engine_instance(engine):
    global _storage_engine_instance
    _storage_engine_instance = engine


class StoragePollingEngine:
    def __init__(self, app, redfish_config):
        self.app = app
        self.config = redfish_config
        self.scheduler = BackgroundScheduler()

    def start(self):
        self.scheduler.add_job(
            self._schedule_due_devices,
            "interval",
            seconds=5,
            id="StoragePollingEngine._schedule_due_devices",
        )
        self.scheduler.start()
        logger.info("Storage polling engine started (checking for due devices every 5s)")

    def _schedule_due_devices(self):
        with self.app.app_context():
            now = datetime.utcnow()
            devices = StorageDevice.query.filter_by(enabled=True).all()
            for device in devices:
                interval = device.polling_interval_seconds or 30
                last = device.last_poll_attempt
                if last is None or (now - last).total_seconds() >= interval:
                    self._poll_device(device.id)

    def poll_one(self, device_id):
        """Called by the /poll-now route for an immediate, on-demand poll."""
        with self.app.app_context():
            self._poll_device(device_id)

    def _poll_device(self, device_id):
        with self.app.app_context():
            device = db.session.get(StorageDevice, device_id)
            if not device:
                return

            device.last_poll_attempt = datetime.utcnow()
            db.session.add(device)
            db.session.commit()

            cipher = get_cipher(self.app.config)
            try:
                password = cipher.decrypt(device.password_encrypted) if device.password_encrypted else None
            except Exception:
                logger.exception("Failed to decrypt password for storage device %s", device.id)
                self._mark_failed(device, "Stored credentials could not be decrypted")
                return

            if not password:
                self._mark_failed(device, "No password stored for this device")
                return

            client = MSAClient(f"https://{device.ip_address}", device.username, password, self.config)

            try:
                components, readings, system_info = collect(client)
            except MSAAuthError as e:
                self._mark_failed(device, f"Authentication failed: {e}", status=ConnectionStatus.AUTH_FAILED)
                return
            except MSAUnreachableError as e:
                self._mark_failed(device, f"Unreachable: {e}", status=ConnectionStatus.UNREACHABLE)
                return
            except Exception as e:
                logger.exception("Unexpected error polling storage device %s", device.id)
                self._mark_failed(device, f"Unexpected error: {e}")
                return

            self._save_success(device, components, readings, system_info)

    def _mark_failed(self, device, error_message, status=ConnectionStatus.UNREACHABLE):
        device.connection_status = status
        device.last_poll_error = error_message
        db.session.add(device)
        db.session.commit()
        logger.warning("Storage device %s poll failed: %s", device.ip_address, error_message)

    def _save_success(self, device, components, readings, system_info):
        device.connection_status = ConnectionStatus.CONNECTED
        device.last_successful_poll = datetime.utcnow()
        device.last_poll_error = None
        device.health_status = system_info.get("health", "Unknown")
        device.vendor = system_info.get("vendor-name") or device.vendor
        device.model = system_info.get("product-id") or device.model
        device.serial_number = system_info.get("midplane-serial-number") or device.serial_number
        db.session.add(device)

        # Replace all components for this device - a full delete-then-insert
        # each cycle, so a component that disappears (e.g. a drive pulled)
        # doesn't linger in the UI forever showing stale "OK" health.
        StorageComponent.query.filter_by(storage_device_id=device.id).delete()
        for c in components:
            db.session.add(StorageComponent(
                storage_device_id=device.id,
                category=c["category"],
                odata_id=c["odata_id"],
                name=c["name"],
                health=c["health"],
                state=c["state"],
                location=c["location"],
                raw_json=c["raw_json"],
                last_updated_at=datetime.utcnow(),
            ))

        for r in readings:
            db.session.add(StorageSensorReading(
                storage_device_id=device.id,
                metric=r["metric"],
                source_name=r["source_name"],
                value=r["value"],
                unit=r["unit"],
            ))

        db.session.commit()
        logger.info(
            "Storage device %s polled successfully: %d components, %d readings",
            device.ip_address, len(components), len(readings),
        )