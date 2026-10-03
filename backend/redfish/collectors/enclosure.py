"""
redfish/collectors/enclosure.py
================================
Collects physical enclosure / JBOD chassis status for SAN storage arrays.

Redfish resources consumed:
- Chassis/{id} -> Enclosures, Fans, PowerSupplies, Thermal, Controllers
"""
import logging
from .common import component, reading, collection_members, unsupported_marker
from database.models import ComponentCategory

logger = logging.getLogger(__name__)


def collect(client, server, topology):
    components, readings = [], []

    chassis_map = topology.get("per_chassis", {})
    for chassis_uri, links in chassis_map.items():
        chassis_doc = client.get(chassis_uri)
        if not chassis_doc:
            continue

        chassis_type = chassis_doc.get("ChassisType") or "Enclosure"
        name = chassis_doc.get("Name") or chassis_doc.get("Id") or "Storage Enclosure"
        odata_id = chassis_doc.get("@odata.id") or chassis_uri

        enriched = dict(chassis_doc)
        
        # Pull sub-resource summaries if available
        if links.get("thermal"):
            thermal_doc = client.get(links["thermal"])
            if thermal_doc:
                enriched["ThermalSummary"] = {
                    "Temperatures": [
                        {
                            "Name": t.get("Name"),
                            "ReadingCelsius": t.get("ReadingCelsius"),
                            "Health": (t.get("Status") or {}).get("Health")
                        }
                        for t in (thermal_doc.get("Temperatures") or [])
                    ],
                    "Fans": [
                        {
                            "Name": f.get("Name"),
                            "ReadingRPM": f.get("Reading"),
                            "Health": (f.get("Status") or {}).get("Health")
                        }
                        for f in (thermal_doc.get("Fans") or [])
                    ]
                }
                for t in (thermal_doc.get("Temperatures") or []):
                    t_name = t.get("Name") or "Enclosure Temp"
                    val = t.get("ReadingCelsius")
                    if val is not None:
                        readings.append(reading("enclosure_temperature", f"{name} ({t_name})", val, "Cel"))

        if links.get("power"):
            power_doc = client.get(links["power"])
            if power_doc:
                enriched["PowerSummary"] = {
                    "PowerSupplies": [
                        {
                            "Name": p.get("Name"),
                            "Health": (p.get("Status") or {}).get("Health"),
                            "State": (p.get("Status") or {}).get("State")
                        }
                        for p in (power_doc.get("PowerSupplies") or [])
                    ]
                }

        components.append(component(
            ComponentCategory.STORAGE_ENCLOSURE, odata_id, name, enriched,
            location=chassis_doc.get("Location") if isinstance(chassis_doc.get("Location"), str) else None
        ))

    readings = [r for r in readings if r]
    return components, readings
