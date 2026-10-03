# Redfish Fleet Monitor

Production-grade server hardware monitoring platform that communicates with servers **exclusively via the Redfish API over HTTPS**. Never uses IPMI or pyghmi. Works with Dell iDRAC, HPE iLO, Lenovo XClarity Controller, Supermicro BMC, and any Redfish-compliant BMC.

---

## Architecture

```
Browser ──WebSocket/REST──> Flask Backend
                                |
                          APScheduler
                                │
                         RedfishClient
                                │
                            HTTPS
                                │
                    iDRAC / iLO / XCC / BMC
                                │
                            Hardware
```

The browser **never** communicates with Redfish/BMC directly. All hardware data flows through the Flask backend which polls BMCs, caches state in PostgreSQL, and pushes live updates to browsers via WebSocket.

---

## Features

- **Vendor-neutral auto-discovery** — starts from `/redfish/v1/` and follows all links. No hardcoded Dell/HPE paths.
- **Redfish Session Authentication** — creates sessions, stores tokens, auto-renews, falls back to Basic Auth
- **13 hardware collectors** — Battery, Chassis, Fans, Memory, Processor, Storage (controller + drives + RAID), Power, Thermal, Voltage, Network, PCIe, Firmware, Security
- **Live WebSocket updates** — browser refreshes without page reload, every 30 seconds
- **Redfish EventService** — subscribes to push events for instant notification when supported
- **Alert engine** — deduplication, auto-resolve, per-category thresholds, temperature/fan/disk/NIC/PSU/battery alerts
- **Historical charts** — temperature, fan RPM, power, voltage, disk wear over 1H/24H/7D/30D
- **Hardware event logs** — SEL, Lifecycle, IML collected from all LogServices
- **Firmware inventory** — via Redfish UpdateService

---

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 14+ (or use Docker Compose)

### 1. Clone and set up

```bash
cd backend/
bash setup.sh        # creates .env with generated keys
```

### 2. Start PostgreSQL (Docker, easiest)

```bash
# From the project root (one level above backend/):
docker compose up -d db
```

Or configure your existing PostgreSQL in `.env`:
```
DATABASE_URL=postgresql://user:password@localhost:5432/redfishmonitor
```

### 3. Install dependencies and run

```bash
cd backend/
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000** in your browser.

### 4. Add your first server

Click **+** in the sidebar and enter:
- **Hostname** — display name for the server
- **BMC IP** — the iDRAC/iLO/XCC management IP address
- **Username / Password** — BMC credentials

The system immediately discovers the Redfish topology and begins polling.

---

## Project Structure

```
backend/
├── app.py                      # Flask factory + all REST routes + SocketIO
├── config.py                   # Configuration (env-driven)
├── requirements.txt
├── setup.sh                    # First-run key generation
├── Dockerfile
├── .env.example
│
├── database/
│   ├── __init__.py             # SQLAlchemy db object
│   └── models.py               # Server, Component, SensorReading, LogEntry, Alert
│
├── auth/
│   └── credentials.py          # Fernet encryption for BMC passwords
│
├── redfish/
│   ├── session.py              # Redfish Session auth + SessionManager
│   ├── client.py               # Resilient GET/POST wrapper with retry
│   ├── discovery.py            # Vendor-neutral topology walker (/redfish/v1/)
│   ├── inventory.py            # Server identity refresh (vendor, model, serial, etc.)
│   ├── events.py               # EventService subscription + webhook parser
│   └── collectors/
│       ├── __init__.py         # COLLECTOR_REGISTRY
│       ├── common.py           # Shared helpers (component(), reading())
│       ├── battery.py          # RAID cache batteries
│       ├── chassis.py          # Chassis identity + intrusion
│       ├── fans.py             # Fan RPM + thresholds
│       ├── memory.py           # DIMM detail + ECC errors
│       ├── processor.py        # CPU socket detail + temp
│       ├── storage.py          # Controllers + drives + RAID volumes
│       ├── power.py            # PSU detail + system wattage
│       ├── thermal.py          # Temperature sensors
│       ├── voltage.py          # Voltage rails
│       ├── network.py          # NICs + network adapters + BMC ports
│       ├── pcie.py             # PCIe devices + functions + slots
│       ├── firmware.py         # Firmware inventory (UpdateService)
│       ├── security.py         # SecureBoot, BIOS, TPM, certificates
│       └── logs.py             # SEL/Lifecycle/IML log entries
│
├── scheduler/
│   └── poller.py               # PollingEngine (ThreadPoolExecutor + APScheduler)
│
├── alerts/
│   └── engine.py               # Alert deduplication + auto-resolve
│
├── websocket/
│   └── events.py               # SocketIO room management + emit helpers
│
├── templates/
│   └── index.html              # SPA shell (served by Flask)
│
└── static/
    ├── css/
    │   └── style.css           # Dark-mode dashboard styles
    └── js/
        └── app.js              # Dashboard logic (no framework, plain DOM + fetch)
```

---

## Configuration Reference

All settings can be set via environment variables or in `.env`.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://redfish:redfish@localhost:5432/redfishmonitor` | PostgreSQL connection string |
| `SECRET_KEY` | (must set) | Flask session secret |
| `ENCRYPTION_KEY` | (auto-generated) | Fernet key for BMC password encryption |
| `REDFISH_VERIFY_TLS` | `false` | Verify BMC TLS certificates |
| `REDFISH_HTTP_TIMEOUT` | `30` | HTTP timeout per request (seconds) |
| `DEFAULT_POLLING_INTERVAL_SECONDS` | `30` | How often to poll each server |
| `MAX_CONCURRENT_POLLS` | `50` | ThreadPoolExecutor max workers |
| `INVENTORY_REFRESH_INTERVAL_SECONDS` | `900` | How often to re-run full discovery |
| `FALLBACK_TEMPERATURE_CRITICAL_C` | `85` | Alert threshold when BMC doesn't advertise one |
| `SENSOR_HISTORY_RETENTION_DAYS` | `30` | How long to keep sensor readings |
| `PUBLIC_WEBHOOK_BASE_URL` | (blank) | Public URL for Redfish EventService push subscriptions |
| `SOCKETIO_MESSAGE_QUEUE` | (blank) | Redis URL for multi-worker SocketIO |

---

## Scaling

For fleets of hundreds/thousands of servers:

1. Set `MAX_CONCURRENT_POLLS=200` (or higher, they're I/O-bound threads)
2. Run Redis: `docker compose --profile redis up -d`
3. Set `SOCKETIO_MESSAGE_QUEUE=redis://localhost:6379/0`
4. Run multiple `gunicorn` workers: `gunicorn -w 4 -k eventlet "app:create_app()[0]"`

---

## REST API

| Method | Path | Description |
|---|---|---|
| GET | `/api/servers` | List all servers |
| POST | `/api/servers` | Add server |
| GET | `/api/servers/{id}` | Server detail |
| PATCH | `/api/servers/{id}` | Update settings |
| DELETE | `/api/servers/{id}` | Remove server |
| POST | `/api/servers/{id}/poll-now` | Trigger immediate poll |
| GET | `/api/servers/{id}/components` | All hardware components (grouped by category) |
| GET | `/api/servers/{id}/history/{metric}` | Sensor time-series (`?range=1h\|24h\|7d\|30d`) |
| GET | `/api/servers/{id}/logs` | Hardware event logs (`?q=search&severity=Critical`) |
| GET | `/api/alerts` | Alert list (`?resolved=true/false`) |
| POST | `/api/alerts/{id}/acknowledge` | Acknowledge alert |
| POST | `/api/alerts/{id}/resolve` | Resolve alert |
| POST | `/api/redfish/webhook` | Redfish EventService inbound webhook |
| GET | `/api/health` | Health check |
