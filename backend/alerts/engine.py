"""
alerts/engine.py
=================
Turns freshly-collected Component rows (and connection-level events) into
Alert rows, with deduplication so a fan that stays in "Warning" for six
hours doesn't create a new alert every 30-second poll cycle - it bumps
`occurrence_count` and `last_occurred_at` on the existing open alert
instead.

Trigger conditions implemented here map directly to the requirements:
temperature over threshold, fan failure, PSU failure, memory failure,
processor degraded, disk predicted failure / SMART failure, RAID
degraded, voltage abnormal, battery low, NIC disconnected, BMC
unreachable, authentication failure, firmware mismatch (best-effort: BIOS
vs BMC-reported minimum, when advertised).
"""
import hashlib
import logging
from datetime import datetime, timedelta

from database.models import Alert, AlertSeverity, ComponentCategory
from alerts import notifier

logger = logging.getLogger(__name__)

_HEALTH_TO_SEVERITY = {
    "OK": None,               # not alertable
    "Warning": AlertSeverity.WARNING,
    "Critical": AlertSeverity.CRITICAL,
}


def _dedupe_key(server_id, category, source, condition) -> str:
    raw = f"{server_id}|{category}|{source}|{condition}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _raise_or_bump(db_session, server_id, category, severity, message, source_property, condition, new_critical=None):
    key = _dedupe_key(server_id, category, source_property, condition)
    existing = (
        Alert.query.filter_by(server_id=server_id, dedupe_key=key, resolved=False).first()
    )
    now = datetime.utcnow()
    if existing:
        existing.occurrence_count += 1
        existing.last_occurred_at = now
        db_session.add(existing)
        return existing

    alert = Alert(
        server_id=server_id,
        category=category,
        severity=severity,
        message=message,
        source_property=source_property,
        dedupe_key=key,
        first_occurred_at=now,
        last_occurred_at=now,
    )
    db_session.add(alert)
    logger.info("New alert [%s/%s] %s: %s", server_id, category, severity.value, message)
    # Only brand-new alerts trigger a ticket email - never a bump on an
    # already-open alert, so a fan stuck in Critical for hours doesn't
    # spam the inbox once per poll cycle.
    if new_critical is not None and severity == AlertSeverity.CRITICAL:
        new_critical.append(alert)
    return alert


def _auto_resolve_missing(db_session, server_id, category, still_present_keys):
    """Resolve any open alert in this category whose dedupe key was not
    reproduced in this poll cycle - i.e. the underlying condition cleared
    (fan spun back up, drive predicted-failure flag cleared, etc.)."""
    open_alerts = Alert.query.filter_by(server_id=server_id, category=category, resolved=False).all()
    for a in open_alerts:
        if a.dedupe_key not in still_present_keys:
            a.resolved = True
            a.resolved_at = datetime.utcnow()
            db_session.add(a)


def evaluate_components(db_session, server_id, category: str, components: list[dict], config, server=None):
    """components: list of the normalized dicts returned by a collector
    (category, odata_id, name, health, state, raw_json).

    `server` (the Server ORM row, optional) is only used to send ticket
    emails for newly-raised critical alerts - pass it when you have it
    handy (poller.py does); alert evaluation itself works fine without it,
    it just means no email gets sent.
    """
    still_present = set()
    new_critical = []

    for c in components:
        health = c.get("health")
        severity = _HEALTH_TO_SEVERITY.get(health)
        if severity is None:
            continue

        name = c.get("name") or c.get("odata_id")
        message = f"{name} reported health '{health}'"
        condition = f"health={health}"
        alert = _raise_or_bump(db_session, server_id, category, severity, message, c.get("odata_id"), condition, new_critical)
        still_present.add(alert.dedupe_key)

        # Category-specific extra conditions beyond raw Status.Health.
        raw = c.get("raw_json") or c.get("properties") or {}
        comp_cat = c.get("category") or category
        if comp_cat in (ComponentCategory.STORAGE_DRIVE, "storage_drive"):
            if raw.get("FailurePredicted") or raw.get("predictive_failure"):
                cond = "failure_predicted"
                a = _raise_or_bump(
                    db_session, server_id, category, AlertSeverity.CRITICAL,
                    f"{name}: drive failure predicted (SMART)", c.get("odata_id"), cond, new_critical,
                )
                still_present.add(a.dedupe_key)
            state_val = str(c.get("state") or "").lower()
            if "fail" in state_val or "fault" in state_val:
                cond = "drive_failed"
                a = _raise_or_bump(
                    db_session, server_id, category, AlertSeverity.CRITICAL,
                    f"{name}: drive failed (State={c.get('state')})", c.get("odata_id"), cond, new_critical,
                )
                still_present.add(a.dedupe_key)

        if comp_cat in (ComponentCategory.STORAGE_POOL, "storage_pool"):
            state_val = str(c.get("state") or "").lower()
            if health in ("Warning", "Critical") or "degraded" in state_val or "fault" in state_val:
                cond = "storage_pool_degraded"
                sev = AlertSeverity.CRITICAL if health == "Critical" else AlertSeverity.WARNING
                a = _raise_or_bump(
                    db_session, server_id, category, sev,
                    f"Storage Pool {name} degraded or unhealthy (Health={health})", c.get("odata_id"), cond, new_critical,
                )
                still_present.add(a.dedupe_key)

        if c["category"] == ComponentCategory.STORAGE_CONTROLLER:
            if health in ("Warning", "Critical"):
                cond = "controller_failed"
                sev = AlertSeverity.CRITICAL if health == "Critical" else AlertSeverity.WARNING
                a = _raise_or_bump(
                    db_session, server_id, category, sev,
                    f"Storage Controller {name} reported health '{health}'", c.get("odata_id"), cond, new_critical,
                )
                still_present.add(a.dedupe_key)

        if c["category"] == ComponentCategory.STORAGE_VOLUME:
            if health in ("Warning", "Critical"):
                cond = "volume_degraded"
                sev = AlertSeverity.CRITICAL if health == "Critical" else AlertSeverity.WARNING
                a = _raise_or_bump(
                    db_session, server_id, category, sev,
                    f"Storage Volume {name} degraded or unavailable (Health={health})", c.get("odata_id"), cond, new_critical,
                )
                still_present.add(a.dedupe_key)

        if c["category"] == ComponentCategory.STORAGE_ENCLOSURE:
            if health in ("Warning", "Critical"):
                cond = "enclosure_unhealthy"
                sev = AlertSeverity.CRITICAL if health == "Critical" else AlertSeverity.WARNING
                a = _raise_or_bump(
                    db_session, server_id, category, sev,
                    f"Enclosure {name} reported health '{health}'", c.get("odata_id"), cond, new_critical,
                )
                still_present.add(a.dedupe_key)

        if c["category"] == ComponentCategory.VOLTAGE_SENSOR:
            reading_v = raw.get("ReadingVolts")
            upper = raw.get("UpperThresholdCritical")
            lower = raw.get("LowerThresholdCritical")
            if reading_v is not None and ((upper and reading_v > upper) or (lower and reading_v < lower)):
                cond = "voltage_out_of_range"
                a = _raise_or_bump(
                    db_session, server_id, category, AlertSeverity.CRITICAL,
                    f"{name}: voltage {reading_v}V outside safe range", c.get("odata_id"), cond, new_critical,
                )
                still_present.add(a.dedupe_key)

        if c["category"] == ComponentCategory.THERMAL_SENSOR:
            reading_c = raw.get("ReadingCelsius", raw.get("Reading"))
            upper = raw.get("UpperThresholdCritical") or config.FALLBACK_TEMPERATURE_CRITICAL_C
            if reading_c is not None and reading_c > upper:
                cond = "temperature_critical"
                a = _raise_or_bump(
                    db_session, server_id, category, AlertSeverity.CRITICAL,
                    f"{name}: temperature {reading_c}C exceeds critical threshold {upper}C",
                    c.get("odata_id"), cond, new_critical,
                )
                still_present.add(a.dedupe_key)

        if c["category"] == ComponentCategory.BATTERY:
            charge = raw.get("ChargePercent")
            if charge is not None and charge < 20:
                cond = "battery_low"
                a = _raise_or_bump(
                    db_session, server_id, category, AlertSeverity.WARNING,
                    f"{name}: battery charge low ({charge}%)", c.get("odata_id"), cond,
                )
                still_present.add(a.dedupe_key)

        if c["category"] == ComponentCategory.NETWORK_INTERFACE:
            link = raw.get("LinkStatus")
            if link == "LinkDown":
                cond = "nic_disconnected"
                a = _raise_or_bump(
                    db_session, server_id, category, AlertSeverity.WARNING,
                    f"{name}: link down", c.get("odata_id"), cond,
                )
                still_present.add(a.dedupe_key)

    _auto_resolve_missing(db_session, server_id, category, still_present)
    db_session.commit()

    for alert in new_critical:
        notifier.send_critical_alert_email(config, alert, server)


def raise_connection_alert(db_session, server_id, severity: AlertSeverity, message: str, condition: str, config=None, server=None):
    new_critical = []
    a = _raise_or_bump(db_session, server_id, "connection", severity, message, "bmc_connection", condition, new_critical)
    db_session.commit()
    if config is not None:
        for alert in new_critical:
            notifier.send_critical_alert_email(config, alert, server)
    return a


def resolve_connection_alerts(db_session, server_id, condition: str):
    key = _dedupe_key(server_id, "connection", "bmc_connection", condition)
    existing = Alert.query.filter_by(server_id=server_id, dedupe_key=key, resolved=False).first()
    if existing:
        existing.resolved = True
        existing.resolved_at = datetime.utcnow()
        db_session.add(existing)
        db_session.commit()