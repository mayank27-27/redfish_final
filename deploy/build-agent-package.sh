#!/usr/bin/env bash
# =============================================================================
# Build Agent Distribution Package
# =============================================================================
# Creates a self-contained agent package (zip) that customers can deploy.
#
# Usage:
#   chmod +x build-agent-package.sh
#   ./build-agent-package.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$SCRIPT_DIR/build/redfish-agent"
VERSION=$(date +%Y%m%d)

echo "Building agent package v$VERSION ..."

# Clean
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/redfish/collectors"

# Copy agent core
cp "$PROJECT_ROOT/backend/agent.py" "$BUILD_DIR/"

# Copy redfish modules
for f in __init__.py session.py client.py discovery.py inventory.py events.py; do
    [ -f "$PROJECT_ROOT/backend/redfish/$f" ] && cp "$PROJECT_ROOT/backend/redfish/$f" "$BUILD_DIR/redfish/"
done

# Copy collectors
cp "$PROJECT_ROOT/backend/redfish/collectors/"*.py "$BUILD_DIR/redfish/collectors/"

# Copy agent package files
cp "$PROJECT_ROOT/agent-package/requirements.txt" "$BUILD_DIR/"
cp "$PROJECT_ROOT/agent-package/README.md" "$BUILD_DIR/"
cp "$PROJECT_ROOT/agent-package/redfish-agent.service" "$BUILD_DIR/"

# Create default agent_config.json template
cat > "$BUILD_DIR/agent_config.json" <<'EOF'
{
  "ENROLLMENT_KEY": "paste-enrollment-key-from-company",
  "AGENT_NAME": "CustomerName-SiteName",
  "CUSTOMER_NAME": "",
  "CUSTOMER_LOCATION": "",
  "DEVICES": [
    {
      "ip_address": "192.168.1.100",
      "username": "root",
      "password": "password",
      "hostname": "server-01"
    }
  ]
}
EOF

# Package
cd "$SCRIPT_DIR/build"
zip -r "redfish-agent-$VERSION.zip" redfish-agent/

echo ""
echo "✅ Agent package built: $SCRIPT_DIR/build/redfish-agent-$VERSION.zip"
echo "   Send this to customers along with the enrollment key."
