from sqlalchemy import create_engine

from app.db import Base
from app.models import ContactAccessAudit, ContactDelivery, ContactGrant, ContactRequest


def test_contact_tables_create_on_sqlite():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        ContactRequest.__table__, ContactGrant.__table__, ContactAccessAudit.__table__, ContactDelivery.__table__,
    ])
