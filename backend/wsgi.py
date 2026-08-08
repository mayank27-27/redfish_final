"""
wsgi.py
========
Entry point for Flask CLI commands (flask db init, flask db migrate, etc.).

Flask's CLI needs FLASK_APP to point to a module that returns a Flask app.
Since the working directory is /app and backend/__init__.py exists, Python
treats /app as a package. This wrapper uses importlib to load app.py directly.

Usage:  FLASK_APP=wsgi:app flask db upgrade
"""
import eventlet
eventlet.monkey_patch()

import importlib.util
import sys
import os

# Load app.py directly (not as part of the 'app' package)
_spec = importlib.util.spec_from_file_location("app_module", os.path.join(os.path.dirname(__file__), "app.py"))
_mod = importlib.util.module_from_spec(_spec)
sys.modules["app_module"] = _mod
_spec.loader.exec_module(_mod)

app = _mod.create_flask_app()
