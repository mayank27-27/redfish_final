"""
redfish/collectors/storage.py
===============================
Redfish resources consumed
---------------------------
- Systems/{id}/Storage (collection) -> Storage/{controller}
  - Storage/{controller}/Drives (collection) -> Drives/{id}
  - Storage/{controller}/Volumes (collection) -> Volumes/{id}  (RAID virtual disks)
- Systems/{id}/SimpleStorage (collection) -> SimpleStorage/{id} (fallback for
  BMCs that don't support the full Storage schema, e.g. some older HPE iLO)

For every storage controller we collect the full controller body (including
RAID level capabilities, supported protocols, firmware version).  For every
physical drive we collect ALL available properties: Capacity, Manufacturer,
Model, SerialNumber, MediaType, Protocol, FirmwareVersion, FailurePredicted,
PredictedMediaLifeLeftPercent (SSD wear), PowerOnHours, RotationSpeedRPM,
CapableSpeedGbs, CapacityBytes, Status, and any OEM fields.  For every
virtual disk (Volume) we capture the RAID type, capacity, and status.

Additional fields collected per drive (extended):
- Predictive Failure  : Drive.FailurePredicted (standard) or
                        OEM.Dell.DellPhysicalDisk.PredictiveFailureState
- Block Size          : Drive.BlockSizeBytes (standard)
- Product ID          : Drive.Model -> Drive.SKU (standard fallback chain)
- Device Description  : Drive.Description (standard)
- Controller          : resolved from the controller body passed at collection time
"""
import logging
from .common import component, reading, collection_members, unsupported_marker
from database.models import ComponentCategory

logger = logging.getLogger(__name__)

_BYTES_TO_GB = 1 / (1024 ** 3)


def collect(client, server, topology):
    components, readings = [], []
    seen_drive_uris = set()
    seen_controller_uris = set()
    seen_pool_uris = set()
    seen_volume_uris = set()

    def _process_controller_body(ctrl, ctrl_name=None):
        ctrl_id = ctrl.get("@odata.id")
        if ctrl_id and ctrl_id not in seen_controller_uris:
            seen_controller_uris.add(ctrl_id)
            c_name = ctrl_name or ctrl.get("Name") or ctrl.get("Model") or ctrl.get("Id") or "Storage Controller"
            components.append(component(
                ComponentCategory.STORAGE_CONTROLLER, ctrl_id, c_name, ctrl,
            ))
            # Process drives linked from controller
            drives_link = ctrl.get("Drives") or []
            if isinstance(drives_link, list):
                for d_ref in drives_link:
                    uri = d_ref.get("@odata.id") if isinstance(d_ref, dict) else (d_ref if isinstance(d_ref, str) else None)
                    if uri and uri not in seen_drive_uris:
                        seen_drive_uris.add(uri)
                        _collect_drive(client, uri, components, readings, c_name)
            elif isinstance(drives_link, dict):
                coll_uri = drives_link.get("@odata.id")
                if coll_uri:
                    for d in collection_members(client, coll_uri):
                        uri = d.get("@odata.id")
                        if uri and uri not in seen_drive_uris:
                            seen_drive_uris.add(uri)
                            _collect_drive_body(d, components, readings, c_name)

    # ── 1. Top-Level Storage Services (MSA 2040 / Redfish Storage Arrays) ──────
    for st_uri in topology.get("storage_services", []):
        st_body = client.get(st_uri)
        if not st_body:
            continue

        st_links = topology.get("per_storage", {}).get(st_uri, {})

        # Storage Controllers
        ctrls_link = st_links.get("storagecontrollers") or st_links.get("controllers") or (st_body.get("StorageControllers") if isinstance(st_body.get("StorageControllers"), str) else None)
        if ctrls_link and isinstance(ctrls_link, str):
            for ctrl in collection_members(client, ctrls_link):
                _process_controller_body(ctrl)
        elif isinstance(st_body.get("StorageControllers"), list):
            for ctrl in st_body.get("StorageControllers", []):
                if isinstance(ctrl, dict):
                    _process_controller_body(ctrl)

        # Drives
        drives_link = st_links.get("drives") or (st_body.get("Drives") if isinstance(st_body.get("Drives"), str) else None)
        if drives_link and isinstance(drives_link, str):
            for d in collection_members(client, drives_link):
                uri = d.get("@odata.id")
                if uri and uri not in seen_drive_uris:
                    seen_drive_uris.add(uri)
                    _collect_drive_body(d, components, readings, controller_name=None)
        elif isinstance(st_body.get("Drives"), list):
            for d_ref in st_body.get("Drives", []):
                uri = d_ref.get("@odata.id") if isinstance(d_ref, dict) else (d_ref if isinstance(d_ref, str) else None)
                if uri and uri not in seen_drive_uris:
                    seen_drive_uris.add(uri)
                    _collect_drive(client, uri, components, readings, controller_name=None)

        # Storage Pools
        pools_link = st_links.get("storagepools") or (st_body.get("StoragePools") if isinstance(st_body.get("StoragePools"), str) else None)
        if pools_link and isinstance(pools_link, str):
            for pool in collection_members(client, pools_link):
                _collect_storage_pool_body(pool, components, readings, seen_pool_uris)

        # Volumes
        vols_link = st_links.get("volumes") or (st_body.get("Volumes") if isinstance(st_body.get("Volumes"), str) else None)
        if vols_link and isinstance(vols_link, str):
            for vol in collection_members(client, vols_link):
                _collect_volume_body(vol, components, readings, seen_volume_uris)

    # Top-level standalone storage pools collection
    for pool_uri in topology.get("storage_pools", []):
        if pool_uri not in seen_pool_uris:
            pool_doc = client.get(pool_uri)
            if pool_doc:
                _collect_storage_pool_body(pool_doc, components, readings, seen_pool_uris)

    # ── 2. System-level Storage ─────────────────────────────────────────
    for system_uri, links in topology.get("per_system", {}).items():

        # Full Storage schema
        storage_uri = links.get("storage")
        if storage_uri:
            for ctrl in collection_members(client, storage_uri):
                ctrl_id   = ctrl.get("@odata.id")
                ctrl_name = ctrl.get("Name") or ctrl.get("Id") or "Storage Controller"
                _process_controller_body(ctrl, ctrl_name)

                # iDRAC 8 fallback: drives under Links.Drives
                links_drives = (ctrl.get("Links") or {}).get("Drives") or []
                for d_ref in links_drives:
                    uri = d_ref.get("@odata.id") if isinstance(d_ref, dict) else None
                    if uri and uri not in seen_drive_uris:
                        seen_drive_uris.add(uri)
                        _collect_drive(client, uri, components, readings, ctrl_name)

                # Virtual disks / Volumes
                volumes_link = (ctrl.get("Volumes") or {}).get("@odata.id")
                if volumes_link:
                    for vol in collection_members(client, volumes_link):
                        _collect_volume_body(vol, components, readings, seen_volume_uris)

                # Storage Pools on controller/system if present
                pools_link = (ctrl.get("StoragePools") or {}).get("@odata.id") if isinstance(ctrl.get("StoragePools"), dict) else None
                if pools_link:
                    for pool in collection_members(client, pools_link):
                        _collect_storage_pool_body(pool, components, readings, seen_pool_uris)

        # SimpleStorage fallback
        simple_uri = links.get("simple_storage")
        if simple_uri:
            for ss in collection_members(client, simple_uri):
                for dev in (ss.get("Devices") or []):
                    odata_id = f"{ss.get('@odata.id')}#device#{dev.get('Name')}"
                    if odata_id in seen_drive_uris:
                        continue
                    seen_drive_uris.add(odata_id)
                    components.append(component(
                        ComponentCategory.STORAGE_DRIVE, odata_id,
                        dev.get("Name") or "Device", dev,
                    ))

        # HPE SmartStorage fallback (iLO 4/5)
        hpe_uri = links.get("smart_storage_hpe") or links.get("storage_hpe")
        if hpe_uri:
            smart_storage = client.get(hpe_uri)
            if smart_storage:
                def _hp_href(body, key):
                    v = (body.get("links") or {}).get(key, {})
                    href = v.get("href")
                    if href:
                        return href
                    return (body.get("Links") or {}).get(key, {}).get("@odata.id")

                controllers_link = _hp_href(smart_storage, "ArrayControllers")
                if controllers_link:
                    for ctrl in collection_members(client, controllers_link):
                        ctrl_id = ctrl.get("@odata.id")
                        if not ctrl_id:
                            continue
                        ctrl_name = ctrl.get("Model") or ctrl.get("Name") or "Smart Array Controller"
                        _process_controller_body(ctrl, ctrl_name)

                        drives_link = _hp_href(ctrl, "PhysicalDrives")
                        if drives_link:
                            for d in collection_members(client, drives_link):
                                uri = d.get("@odata.id")
                                if uri and uri not in seen_drive_uris:
                                    seen_drive_uris.add(uri)
                                    _collect_hp_drive_body(d, components, readings, ctrl_name)

                        logical_link = _hp_href(ctrl, "LogicalDrives")
                        if logical_link:
                            for vol in collection_members(client, logical_link):
                                _collect_volume_body(vol, components, readings, seen_volume_uris)

    # ── 3. Chassis-level drives ─────────────────────────────────────────
    for chassis_uri, chassis_links in topology.get("per_chassis", {}).items():
        drives_uri = chassis_links.get("drives")
        if drives_uri:
            for drive in collection_members(client, drives_uri):
                uri = drive.get("@odata.id")
                if uri and uri not in seen_drive_uris:
                    seen_drive_uris.add(uri)
                    _collect_drive_body(drive, components, readings, controller_name=None)

    # ── 4. Aggregate Capacity Summary & Time-Series Metrics ─────────────
    _compute_capacity_readings(components, readings)

    readings = [r for r in readings if r]

    if not components:
        components.append(unsupported_marker(ComponentCategory.STORAGE_CONTROLLER))

    return components, readings


def _collect_drive(client, uri, components, readings, controller_name=None):
    body = client.get(uri)
    if not body:
        logger.debug("Could not fetch drive body at %s", uri)
        return
    _collect_drive_body(body, components, readings, controller_name)


def _extract_additional_drive_properties(body, controller_name):
    """
    Extract the five additional properties required:
      1. Predictive Failure
      2. Block Size
      3. Product ID
      4. Device Description
      5. Controller

    Field mapping:
      - predictive_failure: Drive.FailurePredicted (standard Redfish)
            fallback: OEM.Dell.DellPhysicalDisk.PredictiveFailureState
            Chosen because FailurePredicted is the standard field; Dell OEM
            provides a more descriptive string ("SmartAlertAbsent") as fallback.
      - block_size_bytes:   Drive.BlockSizeBytes (standard Redfish)
            No OEM fallback needed — universally supported.
      - product_id:         Drive.Model (standard) -> Drive.SKU (standard)
            Model matches iDRAC "Product ID" field exactly (e.g. ST1200MM0108).
            SKU used as secondary fallback per requirements.
      - device_description: Drive.Description (standard Redfish)
            Maps directly to iDRAC "Device Description" field.
            No OEM fallback needed.
      - controller:         passed in from the parent controller's Name field.
            Not available in the Drive resource itself — must be resolved
            from the controller that linked to this drive.
    """
    dell_oem = (body.get("Oem") or {}).get("Dell") or {}
    dell_disk = dell_oem.get("DellPhysicalDisk") or {}

    # 1. Predictive Failure
    predictive_failure = body.get("FailurePredicted")
    if predictive_failure is None:
        # Dell OEM fallback: PredictiveFailureState is a string
        # "SmartAlertAbsent" = no failure predicted, anything else = alert
        oem_state = dell_disk.get("PredictiveFailureState")
        if oem_state is not None:
            predictive_failure = oem_state != "SmartAlertAbsent"
            logger.debug(
                "FailurePredicted not in standard fields for %s — "
                "using Dell OEM PredictiveFailureState: %s",
                body.get("@odata.id"), oem_state
            )
        else:
            logger.debug(
                "FailurePredicted unavailable for %s (no standard or OEM field)",
                body.get("@odata.id")
            )

    # 2. Block Size
    block_size_bytes = body.get("BlockSizeBytes")
    if block_size_bytes is None:
        logger.debug("BlockSizeBytes unavailable for %s", body.get("@odata.id"))

    # 3. Product ID — Model -> SKU fallback chain
    product_id = body.get("Model") or body.get("SKU")
    if product_id is None:
        logger.debug("Product ID unavailable for %s (no Model or SKU)", body.get("@odata.id"))

    # 4. Device Description
    device_description = body.get("Description")
    if device_description is None:
        logger.debug("Description unavailable for %s", body.get("@odata.id"))

    # 5. Controller — passed in from parent, not in drive body
    controller = controller_name
    if controller is None:
        logger.debug("Controller name not passed for drive %s", body.get("@odata.id"))

    return {
        "predictive_failure":  predictive_failure,
        "block_size_bytes":    block_size_bytes,
        "product_id":          product_id,
        "device_description":  device_description,
        "controller":          controller,
    }


def _collect_drive_body(body, components, readings, controller_name=None):
    odata_id = body.get("@odata.id")
    name = (
        body.get("Name")
        or body.get("Model")
        or body.get("Id")
        or "Drive"
    )
    slot = None
    loc = body.get("PhysicalLocation") or body.get("Location") or {}
    if isinstance(loc, dict):
        slot = (loc.get("PartLocation") or {}).get("ServiceLabel") or loc.get("Label")
    if slot:
        name = f"{name} ({slot})"

    # Merge additional properties into the raw body dict so the frontend
    # receives them automatically via raw_json without any API changes.
    extra = _extract_additional_drive_properties(body, controller_name)
    enriched_body = {**body, **extra}

    components.append(component(
        ComponentCategory.STORAGE_DRIVE, odata_id, name, enriched_body, location=slot,
    ))

    # Time-series readings
    wear = body.get("PredictedMediaLifeLeftPercent")
    if wear is not None:
        readings.append(reading("disk_wear", name, wear, "%"))

    temp = body.get("TemperatureCelsius")
    if temp is not None:
        readings.append(reading("disk_temperature", name, temp, "Cel"))


def _collect_hp_drive_body(body, components, readings, controller_name=None):
    """Collect a physical drive body from HP SmartStorage (iLO 4/5 format).

    HP SmartStorage drives use different field names than standard Redfish:
    - CapacityGB / CapacityMiB  instead of CapacityBytes
    - InterfaceType              (SAS, SATA)
    - RotationalSpeedRpm         instead of RotationSpeedRPM
    - FirmwareVersion.Current.VersionString
    - CurrentTemperatureCelsius  instead of TemperatureCelsius
    - Location / LocationFormat  (e.g., "1I:1:1")
    """
    odata_id = body.get("@odata.id")

    # HP drives use Location as a string (e.g. "1I:1:1") not a nested object
    raw_location = body.get("Location") or ""
    model = body.get("Model") or body.get("Name") or "Drive"
    name = f"{model} ({raw_location})" if raw_location else model

    # Normalise capacity to CapacityBytes so existing UI code works
    enriched = dict(body)
    cap_gb = body.get("CapacityGB")
    cap_mib = body.get("CapacityMiB")
    if cap_gb and "CapacityBytes" not in enriched:
        enriched["CapacityBytes"] = int(cap_gb * 1_000_000_000)
    elif cap_mib and "CapacityBytes" not in enriched:
        enriched["CapacityBytes"] = int(cap_mib * 1_048_576)

    # Normalise firmware version
    fw = body.get("FirmwareVersion")
    if isinstance(fw, dict):
        enriched["FirmwareVersion"] = (fw.get("Current") or {}).get("VersionString", "")

    # Controller cross-reference
    enriched["controller"] = controller_name

    # HP predictive failure info
    enriched["predictive_failure"] = None  # not exposed by HP SmartStorage on iLO 4

    components.append(component(
        ComponentCategory.STORAGE_DRIVE, odata_id, name, enriched,
        location=raw_location or None,
    ))

    # Time-series
    temp = body.get("CurrentTemperatureCelsius")
    if temp is not None:
        readings.append(reading("disk_temperature", name, temp, "Cel"))


def _extract_capacity_bytes(obj, keys, gb_keys=None):
    if not isinstance(obj, dict):
        return None
    for k in keys:
        val = obj.get(k)
        if val is not None and isinstance(val, (int, float)):
            return int(val)
    if gb_keys:
        for k in gb_keys:
            val = obj.get(k)
            if val is not None and isinstance(val, (int, float)):
                return int(val * 1_000_000_000)
    # Check nested Capacity dict
    cap_obj = obj.get("Capacity") or {}
    if isinstance(cap_obj, dict):
        data_obj = cap_obj.get("Data") or {}
        if isinstance(data_obj, dict):
            for k in keys:
                val = data_obj.get(k)
                if val is not None and isinstance(val, (int, float)):
                    return int(val)
    return None


def _collect_storage_pool_body(body, components, readings, seen_pool_uris):
    odata_id = body.get("@odata.id")
    if not odata_id or odata_id in seen_pool_uris:
        return
    seen_pool_uris.add(odata_id)

    name = body.get("Name") or body.get("Id") or "Storage Pool"
    
    total_bytes = _extract_capacity_bytes(body, ["TotalBytes", "CapacityBytes", "TotalCapacityBytes"], ["CapacityGB", "TotalCapacityGB"])
    allocated_bytes = _extract_capacity_bytes(body, ["AllocatedBytes", "AllocatedCapacityBytes"])
    used_bytes = _extract_capacity_bytes(body, ["ConsumedBytes", "UsedBytes", "UsedCapacityBytes"])
    free_bytes = _extract_capacity_bytes(body, ["FreeBytes", "FreeCapacityBytes", "UnusedBytes", "UnusedCapacityBytes"])

    if total_bytes and not free_bytes and allocated_bytes:
        free_bytes = max(0, total_bytes - allocated_bytes)
    elif total_bytes and not free_bytes and used_bytes:
        free_bytes = max(0, total_bytes - used_bytes)

    if total_bytes and not used_bytes and free_bytes is not None:
        used_bytes = max(0, total_bytes - free_bytes)

    enriched = dict(body)
    enriched["total_capacity_bytes"] = total_bytes
    enriched["allocated_capacity_bytes"] = allocated_bytes
    enriched["used_capacity_bytes"] = used_bytes
    enriched["free_capacity_bytes"] = free_bytes
    enriched["raid_type"] = body.get("RAIDType") or body.get("PoolType") or (body.get("SupportedRAIDTypes", [None])[0] if isinstance(body.get("SupportedRAIDTypes"), list) else None)

    components.append(component(
        ComponentCategory.STORAGE_POOL, odata_id, name, enriched,
    ))


def _collect_volume_body(body, components, readings, seen_volume_uris):
    odata_id = body.get("@odata.id")
    if not odata_id or odata_id in seen_volume_uris:
        return
    seen_volume_uris.add(odata_id)

    name = body.get("Name") or body.get("LogicalDriveName") or body.get("VolumeName") or body.get("Id") or "Volume"

    cap_bytes = _extract_capacity_bytes(body, ["CapacityBytes", "SizeByte", "SizeBytes"], ["CapacityGB", "SizeGB"])
    allocated_bytes = _extract_capacity_bytes(body, ["AllocatedBytes", "AllocatedCapacityBytes", "CapacityBytesAllocated"])

    enriched = dict(body)
    enriched["capacity_bytes"] = cap_bytes
    enriched["allocated_capacity_bytes"] = allocated_bytes or cap_bytes
    enriched["raid_type"] = body.get("RAIDType") or body.get("VolumeType")
    
    # Associated pool / controller links
    links_dict = body.get("Links") or {}
    pool_link = links_dict.get("StoragePool") or body.get("StoragePool")
    ctrl_link = links_dict.get("StorageController") or body.get("StorageController")
    
    if isinstance(pool_link, dict):
        pool_link = pool_link.get("@odata.id") or pool_link.get("Name")
    if isinstance(ctrl_link, dict):
        ctrl_link = ctrl_link.get("@odata.id") or ctrl_link.get("Name")

    enriched["associated_pool"] = pool_link
    enriched["associated_controller"] = ctrl_link

    components.append(component(
        ComponentCategory.STORAGE_VOLUME, odata_id, name, enriched,
    ))


def _compute_capacity_readings(components, readings):
    raw_physical_bytes = 0
    used_physical_bytes = 0
    free_physical_bytes = 0
    allocated_bytes = 0
    virtual_bytes = 0

    has_pools = False
    for c in components:
        cat = c.get("category")
        raw = c.get("raw_json") or {}

        if cat == ComponentCategory.STORAGE_DRIVE:
            cap = raw.get("CapacityBytes") or (raw.get("CapacityGB", 0) * 1_000_000_000)
            if isinstance(cap, (int, float)):
                raw_physical_bytes += cap

        elif cat == ComponentCategory.STORAGE_POOL:
            has_pools = True
            tot = raw.get("total_capacity_bytes") or 0
            u = raw.get("used_capacity_bytes") or 0
            f = raw.get("free_capacity_bytes") or 0
            a = raw.get("allocated_capacity_bytes") or 0
            
            raw_physical_bytes += tot
            used_physical_bytes += u
            free_physical_bytes += f
            allocated_bytes += a

        elif cat == ComponentCategory.STORAGE_VOLUME:
            cap = raw.get("capacity_bytes") or raw.get("CapacityBytes") or 0
            alloc = raw.get("allocated_capacity_bytes") or cap
            virtual_bytes += cap
            if not has_pools:
                allocated_bytes += alloc

    if raw_physical_bytes > 0:
        readings.append(reading("storage_raw_capacity_gb", "Physical Capacity", round(raw_physical_bytes * _BYTES_TO_GB, 2), "GB"))
    if allocated_bytes > 0:
        readings.append(reading("storage_allocated_capacity_gb", "Allocated Capacity", round(allocated_bytes * _BYTES_TO_GB, 2), "GB"))
    if used_physical_bytes > 0:
        readings.append(reading("storage_used_capacity_gb", "Used Capacity", round(used_physical_bytes * _BYTES_TO_GB, 2), "GB"))
    if free_physical_bytes > 0:
        readings.append(reading("storage_free_capacity_gb", "Unused Capacity", round(free_physical_bytes * _BYTES_TO_GB, 2), "GB"))
    if virtual_bytes > 0:
        readings.append(reading("storage_virtual_capacity_gb", "Logical Capacity", round(virtual_bytes * _BYTES_TO_GB, 2), "GB"))


