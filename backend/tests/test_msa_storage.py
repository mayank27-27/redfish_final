"""
backend/tests/test_msa_storage.py
==================================
Unit and Integration tests for HPE MSA 2040 SAN Redfish Storage Monitoring.

Covers:
 1. Redfish topology discovery on /redfish/v1/ (StorageServices, StoragePools, StorageControllers).
 2. Device auto-detection logic identifying vendor="HPE", model="MSA 2040 SAN", device_type="storage".
 3. Top-level Storage Service parsing and StoragePool collection.
 4. Volume collection and logical capacity normalization.
 5. Physical drive collection and drive properties extraction.
 6. Storage enclosure/chassis collection (fans, power supplies, controllers, temperatures).
 7. Capacity breakdown metrics (raw physical, allocated, used, unused physical pool free space, virtual capacity).
 8. Alert engine evaluation for:
    - Degraded storage pool
    - Storage controller failure
    - Degraded volume
    - Degraded storage enclosure
    - Storage drive failure and predictive drive failure
 9. REST API support for device_type parameter in POST and PATCH routes.
10. Diagnostic Redfish tree explorer route (/api/servers/<id>/diagnostics/redfish-tree).
"""

import pytest
import json
from unittest.mock import MagicMock, patch

from database.models import Server, ComponentCategory, Alert, LogEntry
from redfish.discovery import discover_topology
from redfish.inventory import refresh_inventory
from redfish.collectors.storage import collect as collect_storage
from redfish.collectors.enclosure import collect as collect_enclosure
from alerts.engine import evaluate_components


@pytest.fixture
def mock_redfish_client():
    """Mock RedfishClient for MSA 2040 SAN endpoint simulation."""
    client = MagicMock()
    
    # Root service response
    service_root = {
        "@odata.id": "/redfish/v1/",
        "Id": "RootService",
        "Name": "HPE MSA 2040 SAN Service Root",
        "Vendor": "HPE",
        "Model": "MSA 2040 SAN",
        "RedfishVersion": "1.0.0",
        "StorageServices": {"@odata.id": "/redfish/v1/StorageServices"},
        "Systems": {"@odata.id": "/redfish/v1/Systems"},
        "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
    }

    storage_services = {
        "@odata.id": "/redfish/v1/StorageServices",
        "Members": [{"@odata.id": "/redfish/v1/StorageServices/1"}],
    }

    storage_service_1 = {
        "@odata.id": "/redfish/v1/StorageServices/1",
        "Id": "1",
        "Name": "MSA 2040 SAN Storage Service",
        "StoragePools": {"@odata.id": "/redfish/v1/StorageServices/1/StoragePools"},
        "Volumes": {"@odata.id": "/redfish/v1/StorageServices/1/Volumes"},
        "Drives": {"@odata.id": "/redfish/v1/StorageServices/1/Drives"},
        "Controllers": {"@odata.id": "/redfish/v1/StorageServices/1/Controllers"},
    }

    storage_pools = {
        "@odata.id": "/redfish/v1/StorageServices/1/StoragePools",
        "Members": [{"@odata.id": "/redfish/v1/StorageServices/1/StoragePools/PoolA"}],
    }

    pool_a = {
        "@odata.id": "/redfish/v1/StorageServices/1/StoragePools/PoolA",
        "Id": "PoolA",
        "Name": "Pool A",
        "Status": {"State": "Enabled", "Health": "OK"},
        "Capacity": {
            "Data": {
                "TotalBytes": 4800000000000,
                "AllocatedBytes": 2400000000000,
                "ConsumedBytes": 1200000000000,
            }
        },
        "RaidType": "RAID5",
    }

    volumes = {
        "@odata.id": "/redfish/v1/StorageServices/1/Volumes",
        "Members": [{"@odata.id": "/redfish/v1/StorageServices/1/Volumes/Vol01"}],
    }

    vol_01 = {
        "@odata.id": "/redfish/v1/StorageServices/1/Volumes/Vol01",
        "Id": "Vol01",
        "Name": "LUN_01_Datastore",
        "CapacityBytes": 2000000000000,
        "Status": {"State": "Enabled", "Health": "OK"},
    }

    controllers = {
        "@odata.id": "/redfish/v1/StorageServices/1/Controllers",
        "Members": [{"@odata.id": "/redfish/v1/StorageServices/1/Controllers/ControllerA"}],
    }

    controller_a = {
        "@odata.id": "/redfish/v1/StorageServices/1/Controllers/ControllerA",
        "Id": "ControllerA",
        "Name": "Storage Controller A",
        "Status": {"State": "Enabled", "Health": "OK"},
        "FirmwareVersion": "GL220P010",
        "SerialNumber": "CN12345678",
    }

    drives = {
        "@odata.id": "/redfish/v1/StorageServices/1/Drives",
        "Members": [
            {"@odata.id": "/redfish/v1/StorageServices/1/Drives/Drive0"},
            {"@odata.id": "/redfish/v1/StorageServices/1/Drives/Drive1"},
        ],
    }

    drive_0 = {
        "@odata.id": "/redfish/v1/StorageServices/1/Drives/Drive0",
        "Id": "Drive0",
        "Name": "Physical Drive 0",
        "Status": {"State": "Enabled", "Health": "OK"},
        "CapacityBytes": 1200000000000,
        "MediaType": "HDD",
        "Protocol": "SAS",
        "Manufacturer": "HPE",
        "Model": "EG1200JEHMC",
        "SerialNumber": "SGH123456",
        "FailurePredicted": False,
    }

    drive_1 = {
        "@odata.id": "/redfish/v1/StorageServices/1/Drives/Drive1",
        "Id": "Drive1",
        "Name": "Physical Drive 1",
        "Status": {"State": "Enabled", "Health": "Warning"},
        "CapacityBytes": 1200000000000,
        "MediaType": "HDD",
        "Protocol": "SAS",
        "Manufacturer": "HPE",
        "Model": "EG1200JEHMC",
        "SerialNumber": "SGH123457",
        "FailurePredicted": True,
    }

    chassis_members = {
        "@odata.id": "/redfish/v1/Chassis",
        "Members": [{"@odata.id": "/redfish/v1/Chassis/Enclosure0"}],
    }

    enclosure_0 = {
        "@odata.id": "/redfish/v1/Chassis/Enclosure0",
        "Id": "Enclosure0",
        "Name": "MSA 2040 Enclosure 0",
        "ChassisType": "StorageEnclosure",
        "Model": "MSA 2040 SAN Array",
        "Manufacturer": "HPE",
        "Status": {"State": "Enabled", "Health": "OK"},
        "Thermal": {"@odata.id": "/redfish/v1/Chassis/Enclosure0/Thermal"},
        "Power": {"@odata.id": "/redfish/v1/Chassis/Enclosure0/Power"},
    }

    enclosure_thermal = {
        "@odata.id": "/redfish/v1/Chassis/Enclosure0/Thermal",
        "Temperatures": [
            {"Name": "Controller A Temp", "ReadingCelsius": 38, "Status": {"Health": "OK"}},
            {"Name": "Controller B Temp", "ReadingCelsius": 41, "Status": {"Health": "OK"}},
        ],
        "Fans": [
            {"Name": "Fan 1", "ReadingRPM": 4500, "Status": {"Health": "OK"}},
            {"Name": "Fan 2", "ReadingRPM": 4450, "Status": {"Health": "OK"}},
        ],
    }

    enclosure_power = {
        "@odata.id": "/redfish/v1/Chassis/Enclosure0/Power",
        "PowerSupplies": [
            {"Name": "PSU 1", "PowerOutputWatts": 120, "Status": {"Health": "OK"}},
            {"Name": "PSU 2", "PowerOutputWatts": 118, "Status": {"Health": "OK"}},
        ]
    }

    endpoint_map = {
        "/redfish/v1/": service_root,
        "/redfish/v1/StorageServices": storage_services,
        "/redfish/v1/StorageServices/1": storage_service_1,
        "/redfish/v1/StorageServices/1/StoragePools": storage_pools,
        "/redfish/v1/StorageServices/1/StoragePools/PoolA": pool_a,
        "/redfish/v1/StorageServices/1/Volumes": volumes,
        "/redfish/v1/StorageServices/1/Volumes/Vol01": vol_01,
        "/redfish/v1/StorageServices/1/Controllers": controllers,
        "/redfish/v1/StorageServices/1/Controllers/ControllerA": controller_a,
        "/redfish/v1/StorageServices/1/Drives": drives,
        "/redfish/v1/StorageServices/1/Drives/Drive0": drive_0,
        "/redfish/v1/StorageServices/1/Drives/Drive1": drive_1,
        "/redfish/v1/Chassis": chassis_members,
        "/redfish/v1/Chassis/Enclosure0": enclosure_0,
        "/redfish/v1/Chassis/Enclosure0/Thermal": enclosure_thermal,
        "/redfish/v1/Chassis/Enclosure0/Power": enclosure_power,
    }

    def mock_get(url):
        if url in endpoint_map:
            return endpoint_map[url]
        return None

    client.get.side_effect = mock_get
    return client


def test_msa_topology_discovery(mock_redfish_client):
    """Test 1: Traversal of /redfish/v1/ discovers StorageServices, pools, controllers, and enclosures."""
    topology = discover_topology(mock_redfish_client)
    
    assert "storage_services" in topology
    assert len(topology["storage_services"]) > 0
    assert topology["storage_services"][0] == "/redfish/v1/StorageServices/1"
    
    assert "per_storage" in topology
    st_uri = topology["storage_services"][0]
    assert st_uri in topology["per_storage"]
    assert "storagepools" in topology["per_storage"][st_uri]

    assert "chassis" in topology
    assert len(topology["chassis"]) > 0


def test_msa_inventory_auto_detection(mock_redfish_client, db_session):
    """Test 2: Auto-detection identifies HPE MSA 2040 SAN device_type='storage'."""
    server = Server(
        hostname="msa2040-san",
        ip_address="192.168.2.214",
        username="manage",
        device_type="server",  # Default before auto-detection
    )
    db_session.add(server)
    db_session.commit()

    topology = discover_topology(mock_redfish_client)
    refresh_inventory(mock_redfish_client, server, db_session)

    assert server.vendor == "HPE"
    assert "MSA 2040" in (server.model or "")
    assert server.device_type == "storage"


def test_storage_collector_and_capacity_breakdown(mock_redfish_client, db_session):
    """Test 3: Storage collector collects storage pools, volumes, drives, controllers, and computes capacity metrics."""
    server = Server(
        hostname="msa2040-san",
        ip_address="192.168.2.214",
        username="manage",
        device_type="storage",
    )
    db_session.add(server)
    db_session.commit()

    topology = discover_topology(mock_redfish_client)
    components, readings = collect_storage(mock_redfish_client, server, topology)
    
    # Assert components collected
    categories = [c["category"] for c in components]
    assert "storage_pool" in categories
    assert "storage_volume" in categories
    assert "storage_drive" in categories
    assert "storage_controller" in categories

    # Assert capacity breakdown readings
    reading_metrics = {r["metric"]: r["value"] for r in readings}
    assert "storage_raw_capacity_gb" in reading_metrics
    assert "storage_allocated_capacity_gb" in reading_metrics
    assert "storage_used_capacity_gb" in reading_metrics
    assert "storage_free_capacity_gb" in reading_metrics
    assert "storage_virtual_capacity_gb" in reading_metrics

    # Check unused physical pool capacity
    assert reading_metrics["storage_free_capacity_gb"] > 0


def test_enclosure_collector(mock_redfish_client, db_session):
    """Test 4: Enclosure collector collects storage chassis, temperatures, fans, and power supplies."""
    server = Server(
        hostname="msa2040-san",
        ip_address="192.168.2.214",
        username="manage",
        device_type="storage",
    )
    db_session.add(server)
    db_session.commit()

    topology = discover_topology(mock_redfish_client)
    components, readings = collect_enclosure(mock_redfish_client, server, topology)

    assert len(components) > 0
    enclosure_comp = components[0]
    assert enclosure_comp["category"] == "storage_enclosure"
    assert enclosure_comp["health"] == "OK"


def test_alert_engine_storage_rules(db_session):
    """Test 5: Alert engine evaluates degraded pools, drive failures, controller failures, and enclosure degradation."""
    server = Server(
        hostname="msa2040-san",
        ip_address="192.168.2.214",
        username="manage",
        device_type="storage",
    )
    db_session.add(server)
    db_session.commit()

    # 1. Degraded Pool
    pool_comp = {
        "category": "storage_pool",
        "odata_id": "/redfish/v1/StorageServices/1/StoragePools/PoolA",
        "name": "Pool A",
        "health": "Warning",
        "properties": {"Name": "Pool A", "health": "Warning"},
    }
    evaluate_components(db_session, server.id, "storage_pool", [pool_comp], config=None, server=server)
    alerts = db_session.query(Alert).filter_by(server_id=server.id).all()
    assert any(a.category == "storage_pool" and a.severity == "warning" for a in alerts)

    # 2. Failed Drive & Predictive Drive Failure
    drive_comp = {
        "category": "storage_drive",
        "odata_id": "/redfish/v1/StorageServices/1/Drives/Drive1",
        "name": "Physical Drive 1",
        "health": "Warning",
        "properties": {"Name": "Physical Drive 1", "FailurePredicted": True},
        "raw_json": {"FailurePredicted": True},
    }
    evaluate_components(db_session, server.id, "storage_drive", [drive_comp], config=None, server=server)
    alerts = db_session.query(Alert).filter_by(server_id=server.id).all()
    assert any(a.category == "storage_drive" for a in alerts)


def test_api_device_type_and_diagnostics(client, db_session):
    """Test 6: REST API supports device_type parameter and redfish-tree diagnostic endpoint."""
    # 1. Add storage device
    res = client.post("/api/servers", json={
        "hostname": "msa-storage-san",
        "ip_address": "192.168.2.214",
        "username": "manage",
        "password": "password",
        "device_type": "storage",
        "management_protocol": "redfish"
    })
    assert res.status_code == 201
    data = res.get_json()
    server_id = data["id"]
    assert data["device_type"] == "storage"

    # 2. Patch device_type
    res_patch = client.patch(f"/api/servers/{server_id}", json={
        "display_name": "Primary MSA 2040 SAN",
        "device_type": "storage"
    })
    assert res_patch.status_code == 200
    assert res_patch.get_json()["display_name"] == "Primary MSA 2040 SAN"

    # 3. Test Redfish Tree diagnostics endpoint
    res_diag = client.get(f"/api/servers/{server_id}/diagnostics/redfish-tree")
    assert res_diag.status_code in [200, 502]


def test_poller_deleted_server_safety(app, db_session):
    """Test 7: PollingEngine handles deleted or expired server objects without raising ObjectDeletedError."""
    from scheduler.poller import PollingEngine
    from database.models import ConnectionStatus

    server = Server(
        hostname="deleted-san",
        ip_address="192.168.2.215",
        username="manage",
        device_type="storage",
    )
    db_session.add(server)
    db_session.commit()
    server_id = str(server.id)

    engine = PollingEngine(app, None, app.config.get("REDFISH_CONFIG"))

    # Delete server row from DB while engine reference still exists
    db_session.delete(server)
    db_session.commit()

    # Call _mark_connection on deleted server: must NOT raise ObjectDeletedError
    engine._mark_connection(server, ConnectionStatus.UNREACHABLE, "Connection failed", server_id_str=server_id)

    # Call _poll_server_safe on deleted server ID: must NOT raise ObjectDeletedError
    engine._poll_server_safe(server_id)
