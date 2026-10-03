"""
redfish/discovery.py
=====================
Walks a BMC's Redfish tree starting at the service root
(GET /redfish/v1/) and discovers every resource collection/link it
exposes, entirely generically. No vendor-specific paths are ever
hardcoded - we only ever follow the URIs a BMC itself advertises in its
own JSON (`@odata.id` links), which is exactly what the Redfish
specification requires every compliant implementation to expose.

This makes the same code path work unmodified against Dell iDRAC, HPE
iLO, Lenovo XClarity Controller, Supermicro BMC, or any other
Redfish-conformant implementation - including future ones we've never
tested against.

iDRAC 6 / 7 compatibility additions (non-breaking)
----------------------------------------------------
After the standard link-following pass, we run an inline-summary
extractor for each system body.  iDRAC 7 (Redfish 1.0.x) frequently
exposes ``ProcessorSummary`` and ``MemorySummary`` as inline objects on
the ComputerSystem body **instead of** collection links.  When
``processors`` / ``memory`` links are absent, the extractor stores these
summaries under ``per_system[uri]["processor_summary"]`` /
``per_system[uri]["memory_summary"]`` so that the processor and memory
collectors can synthesize at least one component from the summary data.

HPE iLO 4 compatibility additions (non-breaking)
-------------------------------------------------
HPE iLO 4 (Gen9, Redfish 1.0.x) does not expose storage through the
standard ``Systems/{id}/Storage`` collection.  Instead, it advertises a
proprietary SmartStorage link inside the HPE OEM block of the
ComputerSystem body::

    Oem.Hp.SmartStorage.@odata.id  (early iLO 4 firmware, pre-rebrand)
    Oem.Hpe.SmartStorage.@odata.id (iLO 4 post-rebrand, iLO 5)

When the standard ``storage`` and ``simple_storage`` links are absent,
we extract this OEM link and store it under ``per_system[uri]["storage_hpe"]``
so that ``storage.py`` and ``battery.py`` can reach it without any
hardcoded URI construction.

Similarly, some iLO 4 firmware exposes a ``Memory`` link inside the OEM
block when the standard ``Memory`` top-level link is absent.  We extract
this and store it as the standard ``memory`` key so the existing memory
collector can consume it without modification.

These keys are **additive** — all other existing topology output is
completely unchanged.

Output shape
------------
`discover_topology()` returns a nested dict of URIs (not resource bodies)
describing everything found:

{
  "service_root": {...full GET /redfish/v1 body...},
  "systems": ["/redfish/v1/Systems/System.Embedded.1", ...],
  "chassis": ["/redfish/v1/Chassis/System.Embedded.1", ...],
  "managers": ["/redfish/v1/Managers/iDRAC.Embedded.1", ...],
  "update_service": "/redfish/v1/UpdateService",
  "event_service": "/redfish/v1/EventService",
  "task_service": "/redfish/v1/TaskService",
  "telemetry_service": "/redfish/v1/TelemetryService",
  "per_system": {
      "<system_uri>": {
          "processors": "...", "memory": "...", "storage": "...",
          "simple_storage": "...", "ethernet_interfaces": "...",
          "network_interfaces": "...", "log_services": "...",
          "pcie_devices": "...", "pcie_functions": "...",
          "bios": "...", "secure_boot": "...",
          # HPE iLO 4 OEM storage fallback (absent when standard storage exists):
          "storage_hpe": "..." | None,
          # iDRAC 7 summary fallbacks (absent when collection links exist):
          "processor_summary": {...} | None,
          "memory_summary":    {...} | None,
      }, ...
  },
  "per_chassis": {
      "<chassis_uri>": {
          "power": "...", "thermal": "...", "assembly": "...",
          "network_adapters": "...", "pcie_devices": "...",
          "pcie_slots": "...", "cables": "...", "batteries": "...",
          "log_services": "...",
      }, ...
  },
  "per_manager": {
      "<manager_uri>": {
          "log_services": "...", "ethernet_interfaces": "...",
      }, ...
  },
}
"""
import logging

logger = logging.getLogger(__name__)

# Keys we look for directly on a resource body that point at sub-resources
# or sub-collections. This list is deliberately broad (covers DMTF Redfish
# 1.x schemas for ComputerSystem, Chassis, Manager) but is only ever used
# to *follow links the BMC itself provided* - never to construct a path.
SYSTEM_LINK_KEYS = {
    "Processors": "processors",
    "Memory": "memory",
    "Storage": "storage",
    "SimpleStorage": "simple_storage",
    "StoragePools": "storage_pools",
    "StorageControllers": "storage_controllers",
    "EthernetInterfaces": "ethernet_interfaces",
    "NetworkInterfaces": "network_interfaces",
    "LogServices": "log_services",
    "PCIeDevices": "pcie_devices",
    "PCIeFunctions": "pcie_functions",
    "Bios": "bios",
    "SecureBoot": "secure_boot",
}

CHASSIS_LINK_KEYS = {
    "Power": "power",
    "Thermal": "thermal",
    "Assembly": "assembly",
    "NetworkAdapters": "network_adapters",
    "PCIeDevices": "pcie_devices",
    "PCIeSlots": "pcie_slots",
    "Cables": "cables",
    "LogServices": "log_services",
    "Drives": "drives",
    # Newer (2021+) schema versions split Power/Thermal into these -
    # only present on more recent BMC firmware, harmless if absent.
    "PowerSubsystem": "power_subsystem",
    "ThermalSubsystem": "thermal_subsystem",
    "EnvironmentMetrics": "environment_metrics",
    "Batteries": "batteries",
    "Sensors": "sensors",
}


MANAGER_LINK_KEYS = {
    "LogServices": "log_services",
    "EthernetInterfaces": "ethernet_interfaces",
    "VirtualMedia": "virtual_media",
}


def _odata_id(obj) -> str | None:
    if isinstance(obj, dict):
        return obj.get("@odata.id")
    return None


def _href_or_odata(body: dict, key: str) -> str | None:
    """Return the URI for *key* from either standard Redfish (@odata.id) or
    the HP iLO legacy REST format (links.KEY.href).  The HP format is used
    on iLO 4 (Gen8/Gen9) where sub-resource links are nested under a top-
    level "links" dict with "href" values instead of @odata.id."""
    # Standard: {"Key": {"@odata.id": "/..."}}
    uri = _odata_id(body.get(key)) or _odata_id(body.get("Links", {}).get(key))
    if uri:
        return uri
    # HP iLO 4 legacy: {"links": {"Key": {"href": "/..."}}}
    return body.get("links", {}).get(key, {}).get("href") or None


def _probe_uri(client, uri: str) -> bool:
    """Return True if client.get(uri) returns a non-empty, non-error response."""
    try:
        body = client.get(uri)
        return bool(body and "@odata.id" in body or (body and "Members" in body))
    except Exception:
        return False


def _collection_members(collection_doc: dict | None) -> list[str]:
    if not collection_doc:
        return []
    return [m["@odata.id"] for m in collection_doc.get("Members", []) if "@odata.id" in m]


def discover_topology(client) -> dict:
    service_root = client.get("/redfish/v1/")
    if not service_root:
        # iDRAC 6 and some very early iDRAC 7 firmware do not have a Redfish API at all.
        # Instead of crashing discovery, return an empty topology. The component collectors
        # will gracefully yield "Not Supported" for everything.  The polling engine detects
        # the empty service_root and routes to the WS-Man fallback path for iDRAC 6.
        logger.info(
            "GET /redfish/v1/ returned nothing from %s — "
            "no Redfish API (iDRAC 6 or unreachable). Returning empty topology.",
            client.session.base_url,
        )
        return {
            "service_root": {},
            "systems": [],
            "chassis": [],
            "managers": [],
            "update_service": None,
            "event_service": None,
            "task_service": None,
            "telemetry_service": None,
            "per_system": {},
            "per_chassis": {},
            "per_manager": {}
        }

    # Log capability map at DEBUG level (harmless in production, invaluable when
    # diagnosing iDRAC 7 empty sections with DEBUG logging enabled).
    try:
        from redfish.dell_idrac7_compat import log_service_root_capabilities
        log_service_root_capabilities(service_root, client.session.base_url)
    except Exception:
        pass  # logging helper must never break discovery

    topology = {
        "service_root": service_root,
        "systems": [],
        "chassis": [],
        "managers": [],
        "storage_services": [],
        "storage_pools": [],
        "update_service": _odata_id(service_root.get("UpdateService")),
        "event_service": _odata_id(service_root.get("EventService")),
        "task_service": _odata_id(service_root.get("Tasks")) or _odata_id(service_root.get("TaskService")),
        "telemetry_service": _odata_id(service_root.get("TelemetryService")),
        "per_system": {},
        "per_chassis": {},
        "per_manager": {},
        "per_storage": {},
    }

    systems_uri = _odata_id(service_root.get("Systems"))
    if systems_uri:
        topology["systems"] = _collection_members(client.get(systems_uri))

    chassis_uri = _odata_id(service_root.get("Chassis"))
    if chassis_uri:
        topology["chassis"] = _collection_members(client.get(chassis_uri))

    managers_uri = _odata_id(service_root.get("Managers"))
    if managers_uri:
        topology["managers"] = _collection_members(client.get(managers_uri))

    # Top-level storage collections (MSA 2040 / Redfish Storage Services)
    storage_root_uri = _odata_id(service_root.get("Storage")) or _odata_id(service_root.get("StorageServices"))
    if storage_root_uri:
        topology["storage_services"] = _collection_members(client.get(storage_root_uri))
        if not topology["storage_services"] and _probe_uri(client, storage_root_uri):
            topology["storage_services"] = [storage_root_uri]

    pools_root_uri = _odata_id(service_root.get("StoragePools"))
    if pools_root_uri:
        topology["storage_pools"] = _collection_members(client.get(pools_root_uri))

    # Inspect each top-level storage resource
    for st_uri in topology["storage_services"]:
        body = client.get(st_uri)
        if not body:
            continue
        st_links = {}
        for k in ("Drives", "StorageControllers", "Controllers", "Volumes", "StoragePools", "Enclosures"):
            u = _href_or_odata(body, k)
            if u:
                st_links[k.lower()] = u
        topology["per_storage"][st_uri] = st_links

    for system_uri in topology["systems"]:
        body = client.get(system_uri)
        if not body:
            continue
        links = {}
        for key, out_key in SYSTEM_LINK_KEYS.items():
            uri = _href_or_odata(body, key)
            if uri:
                links[out_key] = uri

        oem = body.get("Oem", {})
        links["oem"] = oem if oem else None

        # ── HP iLO 4 / Gen9 fallback probing ─────────────────────────────
        # iLO 4 does not advertise Memory, Processors, or SmartStorage via
        # standard @odata.id links in the System body.  The paths exist but
        # must be discovered by probing well-known HP sub-paths.
        base = system_uri.rstrip("/")
        _HP_FALLBACKS = {
            "processors":        f"{base}/Processors/",
            "memory":            f"{base}/Memory/",
            "smart_storage_hpe": f"{base}/SmartStorage/",
            "pci_devices_hpe":   f"{base}/PCIDevices/",
            "pci_slots_hpe":     f"{base}/PCISlots/",
        }
        for out_key, candidate in _HP_FALLBACKS.items():
            if out_key not in links:
                if _probe_uri(client, candidate):
                    links[out_key] = candidate
                    logger.debug("HP fallback: %s -> %s", out_key, candidate)

        # Also check HP OEM firmware inventory link on the system
        hpe_fw = (_href_or_odata(body.get("Oem", {}).get("Hp", {}), "FirmwareInventory")
                  or _href_or_odata(body.get("Oem", {}).get("Hpe", {}), "FirmwareInventory"))
        if hpe_fw:
            links["firmware_hpe"] = hpe_fw

        # ── iDRAC 7 inline-summary fallbacks ────────────────────────────
        # iDRAC 7 early Redfish 1.0.x embeds ProcessorSummary / MemorySummary
        # as inline objects on the System body rather than as browsable
        # collection links.  We extract them here so processor.py and memory.py
        # can synthesize at least one component from the summary data.
        # These assignments are ONLY made when the corresponding collection
        # link is absent — iDRAC 8/9 topology is completely unaffected.

        if "processors" not in links:
            ps = body.get("ProcessorSummary") or {}
            if ps:
                links["processor_summary"] = ps
                logger.info(
                    "iDRAC compat [%s]: no Processors collection link — "
                    "found inline ProcessorSummary (Count=%s, Model=%s)",
                    system_uri, ps.get("Count"), ps.get("Model"),
                )
            else:
                logger.debug(
                    "iDRAC compat [%s]: no Processors link AND no ProcessorSummary — "
                    "processor card will show 'Not Supported'", system_uri,
                )

        if "memory" not in links:
            ms = body.get("MemorySummary") or {}
            if ms:
                links["memory_summary"] = ms
                logger.info(
                    "iDRAC compat [%s]: no Memory collection link — "
                    "found inline MemorySummary (TotalSystemMemoryGiB=%s)",
                    system_uri, ms.get("TotalSystemMemoryGiB"),
                )
            else:
                logger.debug(
                    "iDRAC compat [%s]: no Memory link AND no MemorySummary — "
                    "memory card will show 'Not Supported'", system_uri,
                )

        topology["per_system"][system_uri] = links

        # ── HPE iLO 4 OEM fallbacks ──────────────────────────────────────
        # iLO 4 does not expose standard Storage or Memory collection links.
        # Both are advertised inside the Oem.Hp / Oem.Hpe block of the
        # ComputerSystem body.  We extract them here and inject them into
        # the topology under well-known keys so the existing storage.py,
        # battery.py, and memory.py collectors can reach them without any
        # additional HTTP calls or hardcoded URI construction.
        #
        # Guards:
        #   - SmartStorage OEM link is only injected when BOTH the standard
        #     "storage" AND "simple_storage" links are absent, preventing any
        #     risk of overwriting a working standard path on iLO 5/6 or Dell.
        #   - Memory OEM link is only injected when the standard "memory" link
        #     is absent AND the OEM block actually contains a Memory link.
        #   - Both blocks are wrapped in try/except so a malformed OEM block
        #     on a non-HPE server can never break discovery.
        try:
            from redfish.hpe_compat import get_hpe_oem_block, get_smart_storage_uri
            hpe_oem = get_hpe_oem_block(body)

            # SmartStorage OEM storage link (iLO 4 Gen9 primary storage path)
            if "storage" not in links and "simple_storage" not in links:
                ss_uri = get_smart_storage_uri(body)
                if ss_uri:
                    links["storage_hpe"] = ss_uri
                    logger.info(
                        "HPE compat [%s]: standard Storage absent — "
                        "using SmartStorage OEM link: %s",
                        system_uri, ss_uri,
                    )

            # Memory OEM link (some iLO 4 firmware exposes it here)
            if "memory" not in links:
                hpe_mem_link = _odata_id(hpe_oem.get("Memory")) or _odata_id(hpe_oem.get("Links", {}).get("Memory"))
                if hpe_mem_link:
                    links["memory"] = hpe_mem_link
                    logger.info(
                        "HPE compat [%s]: standard Memory absent — "
                        "using OEM Memory link: %s",
                        system_uri, hpe_mem_link,
                    )
        except Exception as _hpe_exc:
            logger.debug(
                "HPE OEM link extraction skipped for %s: %s", system_uri, _hpe_exc
            )

        # Verbose link availability log (DEBUG only)
        try:
            from redfish.dell_idrac7_compat import log_system_link_availability
            log_system_link_availability(system_uri, links)
        except Exception:
            pass

    for chassis_uri_item in topology["chassis"]:
        body = client.get(chassis_uri_item)
        if not body:
            continue
        links = {}
        for key, out_key in CHASSIS_LINK_KEYS.items():
            uri = _href_or_odata(body, key)
            if uri:
                links[out_key] = uri
        topology["per_chassis"][chassis_uri_item] = links

        # Verbose chassis link log (DEBUG only)
        try:
            from redfish.dell_idrac7_compat import log_chassis_link_availability
            log_chassis_link_availability(chassis_uri_item, links)
        except Exception:
            pass

    for manager_uri in topology["managers"]:
        body = client.get(manager_uri)
        if not body:
            continue
        links = {}
        for key, out_key in MANAGER_LINK_KEYS.items():
            uri = _href_or_odata(body, key)
            if uri:
                links[out_key] = uri
        # HP iLO 4: firmware inventory may be under OEM.Hp.links
        hpe_fw = (_href_or_odata(body.get("Oem", {}).get("Hp", {}), "FirmwareInventory")
                  or _href_or_odata(body.get("Oem", {}).get("Hpe", {}), "FirmwareInventory"))
        if hpe_fw:
            links["firmware_hpe"] = hpe_fw
        topology["per_manager"][manager_uri] = links

    logger.info(
        "Discovered %d system(s), %d chassis, %d manager(s) at %s "
        "(UpdateService=%s, EventService=%s)",
        len(topology["systems"]), len(topology["chassis"]), len(topology["managers"]),
        client.session.base_url,
        "yes" if topology.get("update_service") else "no",
        "yes" if topology.get("event_service") else "no",
    )
    return topology
