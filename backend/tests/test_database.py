from sqlalchemy import text
from sqlalchemy.orm import Session
from app.core.database import check_database_health


def test_database_connection(db_session: Session):
    """Test 2: Direct database connection and SQL query execution."""
    result = db_session.execute(text("SELECT 1 AS num")).scalar()
    assert result == 1


def test_check_database_health_function():
    """Verify check_database_health returns status."""
    healthy, msg = check_database_health()
    assert healthy is True
    assert msg == "connected"
