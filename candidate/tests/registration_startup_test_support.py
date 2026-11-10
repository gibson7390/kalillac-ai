"""Synthetic unactivated schema for subprocess tests of unrelated startup features.

The actual AR8 schema probe runs. This helper is never imported by application
code and cannot authorize V2 or supply production keys, receipts or credentials.
"""
def install_unactivated_schema():
    import os
    if os.getenv("KALILLAC_ACCOUNTS_ENABLED","").strip().lower() not in {"1","true","yes","on"}:
        return
    if os.getenv("KALILLAC_DB_ENABLED","").strip().lower() not in {"1","true","yes","on"}:
        return
    if os.getenv("KALILLAC_ACCOUNT_REGISTRATION_V2_ENABLED","").strip().lower() in {"1","true","yes","on"}:
        raise RuntimeError("LEGACY_TEST_SCHEMA_CANNOT_AUTHORIZE_V2")
    try:
        import uuid
        from account_test_db import make_sqlite_engine,make_session_factory
        from kalillac_db.models import Base,AccountRegistrationActivation
        import kalillac_db.engine as engine
    except ImportError:
        return  # The application's existing missing-dependency diagnostic wins.
    fixture_engine=make_sqlite_engine();Base.metadata.create_all(fixture_engine)
    factory=make_session_factory(fixture_engine)
    with factory.begin() as session:
        session.add(AccountRegistrationActivation(singleton_id=1,database_instance_id=uuid.uuid4(),state="unactivated",policy_version=1))
    engine.get_session_factory=lambda:factory
