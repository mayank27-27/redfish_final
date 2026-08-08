import os
import json
import logging
from collections import deque
from database import db
from database.models import Server
from app import create_app
from redfish.client import RedfishClient
from auth.credentials import get_cipher
from redfish.session import SessionManager
from redfish.dell_wsman import WsManClient, DCIM_RESOURCES

logging.basicConfig(level=logging.WARNING)

def crawl_redfish(client, base_url):
    print(f"--- Crawling Redfish for {base_url} ---")
    visited = set()
    queue = deque(["/redfish/v1/"])
    results = {}
    
    while queue:
        path = queue.popleft()
        if path in visited:
            continue
        visited.add(path)
        
        try:
            resp = client._request_response("GET", path)
            status = resp.status_code if resp else "timeout/error"
            if not resp or resp.status_code >= 400:
                print(f"[FAIL] {status} : {path}")
                results[path] = {"status": status}
                continue
            
            body = resp.json()
            if not body:
                print(f"[EMPTY] 200 : {path}")
                results[path] = {"status": 200, "empty": True}
                continue
                
            print(f"[OK] 200 : {path} (@odata.type: {body.get('@odata.type', 'Unknown')})")
            results[path] = {"status": 200, "type": body.get('@odata.type')}
            
            # extract links
            def find_links(obj):
                links = []
                if isinstance(obj, dict):
                    if "@odata.id" in obj:
                        links.append(obj["@odata.id"])
                    for k, v in obj.items():
                        links.extend(find_links(v))
                elif isinstance(obj, list):
                    for item in obj:
                        links.extend(find_links(item))
                return links
            
            for link in find_links(body):
                if link and link.startswith("/redfish/v1/") and link not in visited and link not in queue:
                    # Ignore massive collections if they go too deep, but for iDRAC 7 we want everything
                    if "JsonSchemas" not in link and "Registries" not in link and "SessionService/Sessions" not in link:
                        queue.append(link)
                        
        except Exception as e:
            print(f"[ERROR] {e} : {path}")
            results[path] = {"error": str(e)}
            
    return results

def crawl_wsman(client, base_url):
    print(f"--- Crawling WS-Man for {base_url} ---")
    results = {}
    for name, uris in DCIM_RESOURCES.items():
        if isinstance(uris, str):
            uris = [uris]
        for uri in uris:
            print(f"Enumerate {name} ({uri})...")
            try:
                items = client.enumerate(uri)
                print(f" -> Found {len(items)} items")
                results[name] = {"count": len(items)}
                if items:
                    print(f"    Sample: {json.dumps(items[0])[:200]}")
                    break
            except Exception as e:
                print(f" -> ERROR: {e}")
                results[name] = {"error": str(e)}
            
    # Try alternate DCIM classes commonly used for missing info
    alt_classes = {
        "fan": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/root/dcim/DCIM_FanView",
        "power_supply": "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_PowerSupplyView",
        "battery": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/root/dcim/DCIM_BatteryView",
        "pci": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/root/dcim/DCIM_PCIDeviceView",
        "log": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/root/dcim/DCIM_LifecycleLogView",
    }
    for name, uri in alt_classes.items():
        print(f"Enumerate ALT {name} ({uri})...")
        try:
            items = client.enumerate(uri)
            print(f" -> Found {len(items)} items")
        except Exception as e:
            print(f" -> ERROR: {e}")
            
    return results

def run():
    app, _ = create_app()
    with app.app_context():
        cfg = app.config["REDFISH_CONFIG"]
        cipher = get_cipher(cfg)
        sm = SessionManager(cfg)
        servers = Server.query.all()
        
        for s in servers:
            if not s.password_encrypted:
                print(f"Skipping {s.hostname} — agent-managed (no stored password)")
                continue
            pw = cipher.decrypt(s.password_encrypted)
            base_url = f"https://{s.ip_address}"
            print(f"\n======================================")
            print(f"Testing {s.hostname} ({s.ip_address})")
            print(f"======================================")
            
            # Try WS-Man
            ws_client = WsManClient(s.ip_address, s.username, pw)
            crawl_wsman(ws_client, base_url)
            
            # Try Redfish
            try:
                rf_sess = sm.get_session(s.id, base_url, s.username, pw)
                rf_client = RedfishClient(rf_sess, cfg)
                crawl_redfish(rf_client, base_url)
            except Exception as e:
                print(f"Redfish connection failed: {e}")

if __name__ == "__main__":
    run()
