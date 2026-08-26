"""
storage_devices/msa_collector.py
==================================
Turns MSAClient.get() output into normalized component/reading dicts,
one call per relevant show/* endpoint. Mirrors the shape collectors in
redfish/collectors/*.py use, so the existing UI rendering code (built for
server components) works unchanged for storage device components too.

IMPORTANT: every MSA response includes a trailing <OBJECT basetype="status">
info/success message appended after the real data objects. We filter on
_basetype at each collection point rather than trusting every top-level
object - otherwise that trailing status object gets misread as real
hardware with a fallback odata_id of "unknown".

NOTE on "battery": MSA arrays confirmed (via show/power-supplies and
show/enclosures on real hardware) to have no separate battery component -
write-cache protection is a supercapacitor built into the CompactFlash
module (captured under storage_device_enclosure, "cache-flush" field),
not a discrete battery. No battery category is added here since there is
nothing real to show for it.
"""
import logging
from .msa_client import MSAClient, MSAUnreachableError, MSAAuthError

logger = logging.getLogger(__name__)


def _component(category, odata_id, name, raw, location=None):
    return {
        "category": category,
        "odata_id": odata_id,
        "name": name,
        "health": raw.get("health", "Unknown"),
        "state": raw.get("status", raw.get("state")),
        "location": location,
        "raw_json": raw,
    }


def _reading(metric, name, value, unit):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return {"metric": metric, "source_name": name, "value": value, "unit": unit}


def collect(client: MSAClient):
    """Returns (components: list[dict], readings: list[dict], system_info: dict)."""
    components, readings = [], []

    system_objs = client.get("show/system")
    system_info = next((o for o in system_objs if o.get("_basetype") == "system"), {})

    # ── Disks ──────────────────────────────────────────────────────────
    for d in client.get("show/disks"):
        if d.get("_basetype") != "drives":
            continue
        odata_id = d.get("durable-id", d.get("location", "unknown"))
        name = f"Disk {d.get('location', odata_id)}"
        components.append(_component(
            "storage_device_disk", odata_id, name, d,
            location=d.get("location"),
        ))
        temp = _reading("disk_temperature", name, d.get("temperature-numeric"), "C")
        if temp:
            readings.append(temp)
        hours = _reading("disk_power_on_hours", name, d.get("power-on-hours"), "hours")
        if hours:
            readings.append(hours)

    # ── Controllers (+ nested ports/expander-ports/compact-flash) ───────
    for c in client.get("show/controllers"):
        if c.get("_basetype") != "controllers":
            continue
        ctrl_id = c.get("durable-id", c.get("controller-id", "unknown"))
        ctrl_name = f"Controller {c.get('controller-id', ctrl_id)}"
        components.append(_component("storage_device_controller", ctrl_id, ctrl_name, c))

        for child in c.get("_children", []):
            basetype = child.get("_basetype")
            child_id = child.get("durable-id", f"{ctrl_id}#{basetype}")
            if basetype == "port":
                name = f"Port {child.get('port', child_id)}"
                components.append(_component("storage_device_port", child_id, name, child))
            elif basetype == "expander-ports":
                name = child.get("name", f"Expander {child_id}")
                components.append(_component("storage_device_port", child_id, name, child))
            elif basetype == "compact-flash":
                name = child.get("name", "CompactFlash")
                components.append(_component("storage_device_enclosure", child_id, name, child))

    # ── Power supplies (+ nested fans) ───────────────────────────────────
    for p in client.get("show/power-supplies"):
        if p.get("_basetype") != "power-supplies":
            continue
        psu_id = p.get("durable-id", "unknown")
        psu_name = p.get("name", f"Power Supply {psu_id}")
        components.append(_component(
            "storage_device_power", psu_id, psu_name, p,
            location=p.get("location"),
        ))
        for metric, key, unit in [
            ("psu_voltage_12v", "dc12v", "mV"),
            ("psu_voltage_5v", "dc5v", "mV"),
            ("psu_voltage_3.3v", "dc33v", "mV"),
            ("psu_current_12v", "dc12i", "mA"),
            ("psu_current_5v", "dc5i", "mA"),
            ("psu_temperature", "dctemp", "C"),
        ]:
            r = _reading(metric, psu_name, p.get(key), unit)
            if r:
                readings.append(r)

        for child in p.get("_children", []):
            if child.get("_basetype") != "fan":
                continue
            fan_id = child.get("durable-id", f"{psu_id}#fan")
            fan_name = child.get("name", f"Fan {fan_id}")
            components.append(_component("storage_device_fan", fan_id, fan_name, child))
            speed = _reading("fan_speed", fan_name, child.get("speed"), "RPM")
            if speed:
                readings.append(speed)

    # ── Enclosure / midplane ──────────────────────────────────────────────
    for e in client.get("show/enclosures"):
        if e.get("_basetype") != "enclosures":
            continue
        enc_id = e.get("durable-id", "unknown")
        enc_name = f"Enclosure {e.get('enclosure-id', enc_id)} (Midplane)"
        components.append(_component("storage_device_enclosure", enc_id, enc_name, e))

    return components, readings, system_info


def poll_storage_device(device) -> tuple[bool, str | None]:
    """device: a StorageDevice ORM row (needs .ip_address, .username,
    .password - caller is responsible for decrypting the password first).
    Returns (success, error_message)."""
    client = MSAClient(f"https://{device.ip_address}", device.username, device._decrypted_password, device.config)
    try:
        components, readings, system_info = collect(client)
        return True, None, components, readings, system_info
    except MSAAuthError as e:
        return False, f"Authentication failed: {e}", [], [], {}
    except MSAUnreachableError as e:
        return False, f"Unreachable: {e}", [], [], {}