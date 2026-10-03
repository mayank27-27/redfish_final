# Redfish Fleet Monitor — Agent

The Agent runs on your local network alongside your server BMCs (Dell iDRAC, HPE iLO, Lenovo XCC, etc.) and securely sends hardware telemetry to the central monitoring dashboard.

## Requirements

- Python 3.11+
- Network access to your BMC management IPs (HTTPS port 443)
- Outbound HTTPS access to the monitoring server

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Edit `agent_config.json`:

```json
{
  "ENROLLMENT_KEY": "paste-the-key-provided-by-your-company",
  "AGENT_NAME": "YourCompany-SiteName",
  "CUSTOMER_NAME": "Your Company Name",
  "CUSTOMER_LOCATION": "City, Data Center, Rack",
  "DEVICES": [
    {
      "ip_address": "192.168.1.100",
      "username": "root",
      "password": "your-bmc-password",
      "hostname": "server-name"
    }
  ]
}
```

You can add as many devices as needed.

### 3. Run

```bash
python agent.py
```

On first run, the agent will:
1. Connect to the central monitoring server
2. Automatically register itself
3. Start collecting hardware data from your BMCs
4. Send data securely to the central dashboard

### 4. Run as a Service (Optional)

**Linux (systemd):**
```bash
sudo cp redfish-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now redfish-agent
```

**Windows:**
```powershell
# Run in PowerShell as Administrator
python -m pip install pywin32
python install-service.py install
python install-service.py start
```

## Configuration Reference

| Field | Required | Description |
|---|---|---|
| `ENROLLMENT_KEY` | Yes | Provided by the monitoring company |
| `AGENT_NAME` | Yes | Unique name for this agent (e.g., `AcmeCorp-Mumbai-DC1`) |
| `CUSTOMER_NAME` | No | Your company name |
| `CUSTOMER_LOCATION` | No | Physical location of the servers |
| `DEVICES` | Yes | List of BMC endpoints to monitor |
| `DEVICES[].ip_address` | Yes | BMC management IP address |
| `DEVICES[].username` | Yes | BMC login username |
| `DEVICES[].password` | Yes | BMC login password |
| `DEVICES[].hostname` | No | Display name for this server |

## Troubleshooting

- **"Failed to reach Central Server"** — Check outbound HTTPS (port 443) access
- **"Invalid enrollment key"** — Verify the key with your monitoring provider
- **"Agent name already exists"** — Choose a unique `AGENT_NAME`
- **BMC connection errors** — Verify the BMC IP is reachable and credentials are correct
