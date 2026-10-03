"""
redfish/inventory.py
=====================
Runs once at server-add time and periodically thereafter (see
scheduler/poller.py -> INVENTORY_REFRESH_INTERVAL_SECONDS) to (re)populate
the slow-changing identity fields of a Server row: vendor, model, service
tag, serial number, asset tag, BMC firmware version, and the cached
top-level Redfish URIs (system/chassis/manager) that the live-polling
collectors use so they don't have to re-run full discovery every 30
seconds.

iDRAC 6 / 7 additions
----------------------
- ``get_idrac_generation()`` is called with both ``service_root`` and
  ``manager_body`` so that iDRAC 7 (which often lacks ``Oem.Dell`` in the
  service root) can be identified from the Manager ``Model`` field.
- The detected generation is stored in ``topology["idrac_generation"]`` so
  collectors can read it without making extra HTTP calls.
- ``_guess_vendor()`` already falls through to ``system_body.Manufacturer``
  (which returns "Dell Inc." on iDRAC 7 even without Oem.Dell), so vendor
  detection continues to work unchanged.
"""
import logging
from datetime import datetime

from . import discovery
from .dell_compat import get_idrac_generation

logger = logging.getLogger(__name__)


def _guess_vendor(service_root: dict, system_body: dict | None) -> str | None:
    """Detect the server vendor from the Redfish service root and/or system body.

    Detection is intentionally defensive: Redfish does not mandate a single
    "Vendor" field, so we check every location vendors actually use, in order
    of reliability.

    Normalization rules applied:
    - "Hp" OEM key (HPE iLO 4 early firmware) → "HPE"
    - Manufacturer "HP" or "Hewlett Packard" (older iLO) → "HPE"

    IMPORTANT: use key-existence checks ("X" in oem), NOT truthiness checks
    (oem.get("X") and "Vendor"), because some BMC firmware returns an empty
    dict {} for the OEM block — which is falsy — causing the vendor to be
    missed entirely (iDRAC 8 is a known example of this).
    """
    oem = service_root.get("Oem") or {}

    # Build a labelled candidate list so we can log which source matched.
    # Each entry is (vendor_string, source_label).
    labelled: list[tuple[str, str]] = []

    root_vendor = service_root.get("Vendor")
    if root_vendor:
        labelled.append((root_vendor, "service_root.Vendor"))

    if "Dell" in oem:
        labelled.append(("Dell", "service_root.Oem.Dell"))
    if "Hpe" in oem:
        # HPE iLO 5/6 — OEM key is "Hpe"
        labelled.append(("HPE", "service_root.Oem.Hpe"))
    if "Hp" in oem:
        # HPE iLO 4 early firmware — OEM key is "Hp" (pre-rebrand)
        labelled.append(("HPE", "service_root.Oem.Hp"))
    if "Lenovo" in oem:
        labelled.append(("Lenovo", "service_root.Oem.Lenovo"))
    if "Supermicro" in oem:
        labelled.append(("Supermicro", "service_root.Oem.Supermicro"))

    if system_body:
        mfr = system_body.get("Manufacturer") or ""
        if mfr:
            # Normalize HPE legacy branding variants from the Manufacturer field.
            # iDRAC 7 often lacks Oem.Dell in the service root but returns
            # "Dell Inc." here; older HPE iLO may return "HP" instead of "HPE".
            mfr_norm = mfr
            mfr_lower = mfr.lower()
            if mfr_lower in ("hp", "hewlett packard", "hewlett-packard"):
                mfr_norm = "HPE"
            labelled.append((mfr_norm, f"system_body.Manufacturer ({mfr!r})"))

    for vendor, source in labelled:
        if vendor:
            logger.debug("Vendor detected as %r from %s", vendor, source)
            return vendor

    logger.debug(
        "Vendor could not be determined — service root Oem keys: %s",
        sorted(oem.keys()) or ["(none)"],
    )
    return None


def refresh_inventory(client, server_row, db_session):
    """Discover topology and update the Server row's identity fields.
    Returns the discovered topology dict so callers can reuse it
    immediately for a first collection pass without discovering twice.
    """
    topology = discovery.discover_topology(client)
    service_root = topology["service_root"]

    primary_system_uri = topology["systems"][0] if topology["systems"] else None
    primary_chassis_uri = topology["chassis"][0] if topology["chassis"] else None
    primary_manager_uri = topology["managers"][0] if topology["managers"] else None

    system_body  = client.get(primary_system_uri)  if primary_system_uri  else None
    chassis_body = client.get(primary_chassis_uri) if primary_chassis_uri else None
    manager_body = client.get(primary_manager_uri) if primary_manager_uri else None

    server_row.redfish_service_root = service_root
    server_row.redfish_system_uri   = primary_system_uri
    server_row.redfish_chassis_uri  = primary_chassis_uri
    server_row.redfish_manager_uri  = primary_manager_uri

    server_row.vendor = _guess_vendor(service_root, system_body)

    if system_body:
        server_row.model         = system_body.get("Model")
        server_row.serial_number = system_body.get("SerialNumber")
        server_row.asset_tag     = system_body.get("AssetTag")
        # Dell exposes the service tag as SKU on ComputerSystem; other
        # vendors don't have a separate concept, so we fall back sensibly.
        server_row.service_tag   = system_body.get("SKU") or system_body.get("SerialNumber")
        power_state = system_body.get("PowerState")
        if power_state:
            server_row.power_state = power_state

        status = system_body.get("Status", {}) if isinstance(system_body.get("Status"), dict) else {}
        health = status.get("Health") or status.get("HealthRollup")
        if health:
            server_row.health_status = health

    # Fallback identity & health from chassis / service root / storage for SAN storage devices
    if chassis_body:
        if not server_row.model:
            server_row.model = chassis_body.get("Model")
        if not server_row.health_status or server_row.health_status == "Unknown":
            c_status = chassis_body.get("Status", {}) if isinstance(chassis_body.get("Status"), dict) else {}
            c_health = c_status.get("Health") or c_status.get("HealthRollup")
            if c_health:
                server_row.health_status = c_health
    if not server_row.serial_number and chassis_body:
        server_row.serial_number = chassis_body.get("SerialNumber")
    if not server_row.vendor and service_root.get("Vendor"):
        server_row.vendor = service_root.get("Vendor")

    # Device type detection: automatically classify as 'storage' if model/vendor indicates MSA/SAN or if top-level storage services exist
    if not server_row.device_type or server_row.device_type == "server":
        model_str = (server_row.model or "").lower()
        vendor_str = (server_row.vendor or "").lower()
        if any(kw in model_str or kw in vendor_str for kw in ("msa", "storage", "san", "powervaunt", "storeonce", "array")) or (
            topology.get("storage_services") and not topology.get("systems")
        ):
            server_row.device_type = "storage"
            logger.info("Auto-detected device_type as 'storage' for %s (model=%s, vendor=%s)", server_row.hostname, server_row.model, server_row.vendor)

    if manager_body:
        server_row.firmware_version = manager_body.get("FirmwareVersion")

    server_row.supports_event_service = bool(topology.get("event_service"))
    server_row.updated_at = datetime.utcnow()

    # ── iDRAC generation detection ────────────────────────────────────────
    # Pass manager_body as the second argument so that iDRAC 7 (which often
    # lacks Oem.Dell in the service root) can be identified from the Manager
    # Model field (e.g. "Integrated Dell Remote Access Controller 7").
    # The generation is stored in the topology dict so collectors can read
    # it without additional HTTP calls.
    generation = get_idrac_generation(service_root, manager_body)
    topology["idrac_generation"] = generation

    # ── HPE iLO generation detection ─────────────────────────────────────────
    from .hpe_ilo_compat import get_ilo_generation
    ilo_generation = None
    if not service_root or (server_row.vendor and ("hpe" in server_row.vendor.lower() or "hp" in server_row.vendor.lower())):
        ilo_generation = get_ilo_generation(
            service_root, server_row.ip_address, client.config.get("VERIFY_TLS", False)
        )
    topology["ilo_generation"] = ilo_generation

    if generation == "idrac7":
        try:
            from .dell_idrac7_compat import crawl_and_map_missing_links
            crawl_and_map_missing_links(client, topology, client.session.base_url)
        except Exception as exc:
            logger.warning("Failed to run iDRAC 7 crawler for %s: %s", server_row.hostname, exc)

    if generation:
        logger.info(
            "iDRAC generation detected for %s (%s): %s "
            "(RedfishVersion=%s, vendor=%s)",
            server_row.hostname, server_row.ip_address,
            generation,
            service_root.get("RedfishVersion", "N/A"),
            server_row.vendor,
        )
    else:
        logger.debug(
            "iDRAC generation could not be determined for %s (%s) — "
            "treating as non-Dell or unknown.",
            server_row.hostname, server_row.ip_address,
        )

    db_session.add(server_row)
    db_session.commit()

    logger.info(
        "Inventory refreshed for %s (%s): vendor=%s model=%s serial=%s generation=%s",
        server_row.hostname, server_row.ip_address,
        server_row.vendor, server_row.model, server_row.serial_number, generation,
    )
    return topology
