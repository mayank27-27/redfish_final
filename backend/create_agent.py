import hashlib
import secrets
import sys
try:
    # When running locally from redfish_new/ root
    from backend.app import create_app
    from backend.database import db
    from backend.database.models import Agent, Site, Server
except ImportError:
    # When running inside Docker where /app is mapped to backend/
    from app import create_app
    from database import db
    from database.models import Agent, Site, Server

app, socketio = create_app()

def create_agent(name):
    with app.app_context():
        # Generate a secure token
        raw_token = secrets.token_hex(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        # Create Agent
        agent = Agent(name=name, api_key_hash=token_hash)
        db.session.add(agent)
        db.session.commit()

        print(f"\n✅ Agent '{name}' created successfully!")
        print("-" * 50)
        print(f"Agent ID:    {agent.id}")
        print(f"Agent Token: {raw_token}")
        print("-" * 50)
        print("⚠️  SAVE THIS TOKEN! It will not be shown again.\n")
        return agent.id

def list_servers():
    with app.app_context():
        servers = Server.query.all()
        if not servers:
            print("No servers found in the database. Add one via the Dashboard first.")
            return

        print("\n--- Available Servers ---")
        for s in servers:
            print(f"Name: {s.display_name or s.hostname} | IP: {s.ip_address} | Server ID: {s.id}")
        print("-------------------------\n")

def generate_enrollment_key():
    """Generate an enrollment key and its hash for .env configuration."""
    raw_key = secrets.token_hex(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    print(f"\n✅ Enrollment key generated!")
    print("-" * 60)
    print(f"Enrollment Key (share with customers):  {raw_key}")
    print(f"Enrollment Key Hash (put in .env):      {key_hash}")
    print("-" * 60)
    print(f"\nAdd this to your backend/.env file:")
    print(f"ENROLLMENT_KEY_HASH={key_hash}")
    print(f"\n⚠️  Share the Enrollment Key (not the hash) with customers.\n")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python create_agent.py --create <agent_name>")
        print("  python create_agent.py --list-servers")
        print("  python create_agent.py --gen-enrollment-key")
        sys.exit(1)

    action = sys.argv[1]
    if action == "--create" and len(sys.argv) == 3:
        create_agent(sys.argv[2])
    elif action == "--list-servers":
        list_servers()
    elif action == "--gen-enrollment-key":
        generate_enrollment_key()
    else:
        print("Invalid arguments.")
