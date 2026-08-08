#!/bin/bash
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# startup.sh â€” Auto-migrate database, then start the Flask app
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
set -e

export FLASK_APP=wsgi

echo "â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•"
echo "  Database Migration & Schema Readiness Check"
echo "â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•"

# Execute python migration and legacy alignment script
python3 << 'MIGRATE_EOF'
import sys
from wsgi import app
from flask_migrate import upgrade, stamp
from sqlalchemy import inspect, text
from database import db

with app.app_context():
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()

    has_alembic = "alembic_version" in tables
    user_tables = [t for t in tables if t != "alembic_version"]
    has_tables = len(user_tables) > 0

    if has_tables and not has_alembic:
        print("[migrate] Legacy untracked database detected (created via db.create_all).")
        print("[migrate] Inspecting legacy schema for column & type parity...")
        
        # 1. Ensure required columns exist on 'servers' table safely (idempotent)
        server_cols = {c["name"]: c for c in inspector.get_columns("servers")} if "servers" in user_tables else {}

        with db.engine.connect() as conn:
            # Check missing columns on servers table
            columns_to_add = [
                ("customer_name", "TEXT"),
                ("customer_location", "TEXT"),
                ("maintenance_records", "JSONB"),
                ("management_protocol", "VARCHAR(50) DEFAULT 'redfish'"),
            ]
            for col_name, col_type in columns_to_add:
                if col_name not in server_cols:
                    print(f"[migrate] Repairing legacy schema: ADD COLUMN {col_name} to servers...")
                    conn.execute(text(f"ALTER TABLE servers ADD COLUMN IF NOT EXISTS {col_name} {col_type};"))
            
            # Check if id column is varchar instead of uuid on PostgreSQL
            if "id" in server_cols:
                col_type_str = str(server_cols["id"]["type"]).upper()
                if "VARCHAR" in col_type_str or "CHAR" in col_type_str:
                    print("[migrate] Repairing legacy schema: converting servers.id from VARCHAR to UUID...")
                    conn.execute(text("ALTER TABLE servers ALTER COLUMN id TYPE UUID USING id::UUID;"))
            
            conn.commit()

        print("[migrate] Legacy schema repair complete. Stamping baseline head revision...")
        stamp(revision="head")
        print("[migrate] Database successfully aligned and stamped at head.")

    elif not has_tables:
        print("[migrate] Fresh database detected. Running baseline upgrade...")
        upgrade()
        print("[migrate] Full migration applied successfully.")

    else:
        print("[migrate] Migration-tracked database detected. Running pending upgrades...")
        upgrade()
        print("[migrate] Database is up to date.")

MIGRATE_EOF

echo "â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•"

# Start the application using wsgi entrypoint
exec python -c "import wsgi; exec(open('app.py').read())"

