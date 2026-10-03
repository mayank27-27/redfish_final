"""
redfish/collectors/
====================
Each module in this package is responsible for exactly one card in the UI
(Battery, Chassis, Fans, Memory, Processor, Storage, Power, Thermal,
Voltage, Network, PCIe, Firmware, Logs, Security) and exactly one
question: "given a client and the discovered topology for this server,
what does Redfish currently say about this category?"

Every collector returns a plain list of dicts shaped like:
    {
        "category": ComponentCategory.<X>,
        "odata_id": "...",       # stable identity for upsert
        "name": "...",
        "health": "OK" | "Warning" | "Critical" | None,
        "state": "Enabled" | "Absent" | ... | None,
        "location": "..." | None,
        "raw_json": {...the full Redfish resource or member...},
    }

and, where the category has values worth charting over time, a second
list of sensor reading dicts:
    {"metric": "...", "source_name": "...", "value": float, "unit": "..."}

collectors/logs.py is handled slightly differently since it returns
LogEntry-shaped dicts (not Component-shaped) - see that module.

poller.py drives all of this; nothing here talks to the database or to
SocketIO directly, which keeps collectors independently testable against
a mocked RedfishClient.
"""
from . import (
    battery,
    chassis,
    fans,
    memory,
    processor,
    storage,
    enclosure,
    power,
    thermal,
    voltage,
    network,
    pcie,
    firmware,
    security,
    logs,
)

# category name -> module, used by poller.py to iterate deterministically
# and by the API layer to validate ?category= query params.
COLLECTOR_REGISTRY = {
    "battery":   battery,
    "chassis":   chassis,
    "fans":      fans,
    "memory":    memory,
    "processor": processor,
    "storage":   storage,
    "enclosure": enclosure,
    "power":     power,
    "thermal":   thermal,
    "voltage":   voltage,
    "network":   network,
    "pcie":      pcie,
    "firmware":  firmware,
    "security":  security,
}
