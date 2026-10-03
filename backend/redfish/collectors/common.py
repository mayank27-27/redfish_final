"""
redfish/collectors/common.py
=============================
Small normalization helpers shared by every collector, so category
modules stay focused on "which Redfish endpoints do I read" rather than
repeating boilerplate for pulling Status.Health/State out of a resource.
"""


def status_health(resource: dict) -> str | None:
    status = resource.get("Status")
    if isinstance(status, dict):
        return status.get("Health") or status.get("HealthRollup")
    return None


def status_state(resource: dict) -> str | None:
    status = resource.get("Status")
    if isinstance(status, dict):
        return status.get("State")
    return None


def component(category, odata_id, name, raw_json, location=None, health=None, state=None):
    return {
        "category": category,
        "odata_id": odata_id,
        "name": name,
        "health": health if health is not None else status_health(raw_json),
        "state": state if state is not None else status_state(raw_json),
        "location": location,
        "raw_json": raw_json,
    }


def unsupported_marker(category):
    """Return a dummy component representing an unsupported collection.
    The UI will detect 'meta:unsupported' and hide the '0' count."""
    return component(category, "meta:unsupported", "Not Supported", {})


def reading(metric, source_name, value, unit=None):
    if value is None:
        return None
    return {"metric": metric, "source_name": source_name, "value": float(value), "unit": unit}


def collection_members(client, collection_uri):
    """GET a Redfish collection and return the list of full member
    resources (not just their URIs) - this is the shape almost every
    collector needs."""
    import logging
    logger = logging.getLogger(__name__)

    if not collection_uri:
        return []
    
    logger.info("Fetching collection: %s", collection_uri)
    coll = client.get(collection_uri)
    if not coll:
        logger.info("Collection missing or empty: %s", collection_uri)
        return []
    
    members = []
    
    while coll:
        for m in coll.get("Members", []):
            uri = m.get("@odata.id")
            if not uri:
                continue
            body = client.get(uri)
            if body:
                logger.info("Endpoint URL: %s | Status: OK | Exists: True", uri)
                if "@odata.id" not in body:
                    body["@odata.id"] = uri
                members.append(body)
            else:
                logger.warning("Endpoint URL: %s | Status: FAILED | Exists: False", uri)
                
        next_link = coll.get("Members@odata.nextLink")
        if next_link:
            coll = client.get(next_link)
        else:
            break
            
    return members
