import os
import pytest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret-key"

from app import create_app
from database import db as _db

@pytest.fixture(scope="session")
def app():
    """Create and configure a new Flask app instance for testing."""
    app, _ = create_app()
    app.config["TESTING"] = True

    with app.app_context():
        _db.create_all()
        yield app
        _db.drop_all()

@pytest.fixture
def db_session(app):
    """Provide a clean database session per test."""
    with app.app_context():
        _db.session.rollback()
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()
        yield _db.session
        _db.session.rollback()
        _db.session.remove()

@pytest.fixture
def client(app):
    """A test client for the app."""
    return app.test_client()
