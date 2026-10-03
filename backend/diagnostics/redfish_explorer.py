"""
diagnostics/redfish_explorer.py
================================
Diagnostic / debug explorer for inspecting Redfish resources exposed by storage devices
(e.g., HPE MSA 2040 SAN) or servers.

Discovers:
- Service Root (/redfish/v1/)
- Top-level resources (Systems, Chassis, Managers, Storage, StorageServices, StoragePools)
- Storage endpoints (Controllers, Drives, Pools, Volumes, Enclosures)
- Raw JSON keys and structure returned by each endpoint
"""
import sys
import json
import logging
import argparse

logger = logging.getLogger(__name__)

def explore_redfish(client):
    """Walk available Redfish endpoints and capture diagnostic tree summary."""
    report = {
        "service_root": None,
        "discovered_endpoints": {},
        "storage_hierarchy": {
            "systems": [],
            "chassis": [],
            "storage_services": [],
            "storage_pools": [],
            "controllers": [],
            "drives": [],
            "volumes": []
        },
        "errors": []
    }

    try:
        root = client.get("/redfish/v1/")
        report["service_root"] = root or {}
        if not root:
            report["errors"].append("GET /redfish/v1/ returned empty or None")
            return report

        endpoints_to_check = [
            "/redfish/v1/",
            "/redfish/v1/Systems",
            "/redfish/v1/Chassis",
            "/redfish/v1/Managers",
            "/redfish/v1/Storage",
            "/redfish/v1/StorageServices",
            "/redfish/v1/StoragePools",
            "/redfish/v1/StorageControllers",
            "/redfish/v1/Drives",
            "/redfish/v1/Volumes"
        ]

        for ep in endpoints_to_check:
            res = client.get(ep)
            if res:
                report["discovered_endpoints"][ep] = {
                    "status": "available",
                    "odata_id": res.get("@odata.id"),
                    "name": res.get("Name"),
                    "keys": list(res.keys()),
                    "members_count": len(res.get("Members", [])) if "Members" in res else None
                }

                # If collection, probe member details
                members = res.get("Members", []) if isinstance(res.get("Members"), list) else []
                for m in members:
                    m_uri = m.get("@odata.id") if isinstance(m, dict) else None
                    if m_uri:
                        m_doc = client.get(m_uri)
                        if m_doc:
                            report["discovered_endpoints"][m_uri] = {
                                "status": "available",
                                "odata_id": m_doc.get("@odata.id"),
                                "name": m_doc.get("Name"),
                                "model": m_doc.get("Model"),
                                "health": (m_doc.get("Status") or {}).get("Health"),
                                "keys": list(m_doc.keys())
                            }
                            if "Systems" in ep: report["storage_hierarchy"]["systems"].append(m_uri)
                            elif "Chassis" in ep: report["storage_hierarchy"]["chassis"].append(m_uri)
                            elif "Storage" in ep: report["storage_hierarchy"]["storage_services"].append(m_uri)
                            elif "StoragePools" in ep: report["storage_hierarchy"]["storage_pools"].append(m_uri)
                            elif "Drives" in ep: report["storage_hierarchy"]["drives"].append(m_uri)
                            elif "Volumes" in ep: report["storage_hierarchy"]["volumes"].append(m_uri)

            else:
                report["discovered_endpoints"][ep] = {"status": "unavailable (404/501)"}

    except Exception as exc:
        report["errors"].append(str(exc))

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HPE MSA / Redfish Diagnostic Explorer")
    parser.add_argument("--host", required=True, help="IP or hostname of storage device")
    parser.add_argument("--user", required=True, help="Redfish Username")
    parser.add_argument("--password", required=True, help="Redfish Password")
    args = parser.parse_args()

    from redfish.session import RedfishSession
    from redfish.client import RedfishClient
    class DummyConfig:
        REDFISH_VERIFY_TLS = False
        REDFISH_HTTP_TIMEOUT = 10.0
        REDFISH_MAX_RETRIES = 2
        REDFISH_RETRY_BACKOFF_SECONDS = 1.0

    cfg = DummyConfig()
    base_url = f"https://{args.host}"
    session = RedfishSession(base_url, args.user, args.password, cfg, "diag")
    client = RedfishClient(session, cfg)

    print(f"Connecting to {base_url}...")
    res = explore_redfish(client)
    print(json.dumps(res, indent=2))
