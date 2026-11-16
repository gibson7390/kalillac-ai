"""The application's private connection to the V1 account-security authorities (V1-02D, step D1).

S, the service role, may do exactly one thing in the database: call the fixed SECURITY DEFINER
functions that E owns. This module owns the only engine logged in as S. It calls those functions with
bound parameters and nothing else; no generic SQL path is reachable from it, the session factory is
private, and every result is checked against the function's exact contract before it is returned.

Building it (build_v1_authority) refuses unless:

* the credential, read through the strict protected-file loader in production, names exactly this
  role, database instance and deployment, carries a 32-byte random password in canonical base64url
  form, and differs from the runtime login's password;
* every pooled connection is S itself (session_user, current_user and, in production, a SCRAM login
  as S), checked again on each checkout;
* S holds no role attribute but LOGIN, no membership, no schema CREATE and no right on any Kalillac
  table or column; it can execute exactly the reviewed authority functions in the Kalillac schema and
  no SECURITY DEFINER function anywhere else; and each of those functions is owned by E, runs with
  the fixed search_path, has the reviewed body (by SHA-256) and is executable by E and S only.

It is not wired into application startup yet. S stays NOLOGIN until the operator transition (E2)
gives it a login and a credential and moves the activation latch to active, and V1 startup still
refuses an active catalog (v1_schema.require_qualified_authority) until that transition's
qualification is implemented. Nothing here changes a role, a grant or the latch.

Failures carry no detail: never a password, a token hash, SQL, parameters or a driver message.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import re
import uuid

CREDENTIAL_FILE_ENV = "KALILLAC_V1_SERVICE_CREDENTIALS_FILE"
CREDENTIAL_MAX_BYTES = 8192
POOL_SIZE = 3
POOL_TIMEOUT_SECONDS = 3
STATEMENT_TIMEOUT = "5s"
LOCK_TIMEOUT = "3s"
MAX_LIFETIME_DAYS = 366

ISSUE_OUTCOMES = frozenset({"issued", "not_authenticated", "security_unavailable"})
END_OUTCOMES = frozenset({"ended", "security_unavailable"})
REVOKE_OUTCOMES = frozenset({"revoked", "not_authenticated", "account_unavailable", "too_many_operations",
                             "security_unavailable"})
REVOKE_SCOPES = ("others", "all")
RESET_OUTCOMES = frozenset({"reset", "invalid_token", "account_unavailable", "too_many_operations",
                            "security_unavailable"})
CHANGE_OUTCOMES = frozenset({"changed", "not_authenticated", "account_unavailable", "too_many_operations",
                             "security_unavailable"})

# The only statements this module sends as S, besides its fixed transaction settings and identity and
# catalog checks. Function arguments are bound, never interpolated.
_ISSUE = "SELECT outcome, issued_at, valid_until, replayed FROM kalillac.v1_issue_session(:s, :t, :u, :h, :d)"
_REVOKE = "SELECT outcome, retry_after_seconds, replayed FROM kalillac.v1_revoke_sessions(:o, :s, :t, :c)"
_END = "SELECT outcome FROM kalillac.v1_end_session(:s, :t)"
_RESET = "SELECT outcome, retry_after_seconds, replayed FROM kalillac.v1_reset_password(:o, :t, :h)"
_CHANGE = ("SELECT outcome, retry_after_seconds, replayed"
           " FROM kalillac.v1_change_password(:o, :s, :t, :v, :h)")
# The Argon2id encoding the application's hasher produces; the function re-checks the same shape.
_ARGON2ID = re.compile(r"\$argon2id\$v=19\$m=[0-9]{1,10},t=[0-9]{1,10},p=[0-9]{1,3}\$[A-Za-z0-9+/]{11,}"
                       r"\$[A-Za-z0-9+/]{16,}")
_TOKEN_HASH = re.compile(r"[0-9a-f]{64}")


class V1AuthorityUnavailable(RuntimeError):
    """The authority cannot be used or did not answer within its contract. Carries no detail."""

    def __init__(self):
        super().__init__("V1 account authority unavailable.")


@dataclass(frozen=True)
class SessionIssued:
    outcome: str                       # issued | not_authenticated | security_unavailable
    issued_at: datetime | None
    valid_until: datetime | None
    replayed: bool


@dataclass(frozen=True)
class SessionsRevoked:
    outcome: str                       # revoked | not_authenticated | account_unavailable | too_many_operations | security_unavailable
    retry_after_seconds: int | None    # only with too_many_operations
    replayed: bool


@dataclass(frozen=True)
class PasswordChanged:
    outcome: str                       # changed | not_authenticated | account_unavailable | too_many_operations | security_unavailable
    retry_after_seconds: int | None    # only with too_many_operations
    replayed: bool


@dataclass(frozen=True)
class PasswordReset:
    outcome: str                       # reset | invalid_token | account_unavailable | too_many_operations | security_unavailable
    retry_after_seconds: int | None    # only with too_many_operations
    replayed: bool


def _reviewed_functions():
    """{signature: SHA-256 of the reviewed body} for the authorities S may execute at this release's
    head (0020): revocation v2, issuance, sign-out, password reset and password change."""
    from .v1_schema import (CHANGE_PASSWORD_SIGNATURE, CHANGE_PASSWORD_SOURCE_SHA256, END_SESSION_SIGNATURE,
                            END_SESSION_SOURCE_SHA256, ISSUANCE_SIGNATURE, ISSUANCE_SOURCE_SHA256,
                            RESET_PASSWORD_SIGNATURE, RESET_PASSWORD_SOURCE_SHA256, REVOCATION_SIGNATURE,
                            REVOCATION_SOURCE_SHA256_V2)
    return {REVOCATION_SIGNATURE: REVOCATION_SOURCE_SHA256_V2, ISSUANCE_SIGNATURE: ISSUANCE_SOURCE_SHA256,
            END_SESSION_SIGNATURE: END_SESSION_SOURCE_SHA256,
            RESET_PASSWORD_SIGNATURE: RESET_PASSWORD_SOURCE_SHA256,
            CHANGE_PASSWORD_SIGNATURE: CHANGE_PASSWORD_SOURCE_SHA256}


def parse_service_credential(raw, *, role, database_instance_id, deployment_id, forbidden_passwords=()):
    """The S password from a protected credential file's bytes, or V1AuthorityUnavailable.

    The file is exactly {"format_version": 1, "role", "database_instance_id", "deployment_id",
    "password"}: strict JSON, no duplicate or extra key, bound to this role, database instance and
    deployment, and a password that is 32 random bytes in canonical unpadded base64url and is not one
    of forbidden_passwords (the runtime login's)."""
    from .registration_crypto import strict_json
    try:
        credential = strict_json(raw)
    except Exception:
        raise V1AuthorityUnavailable() from None
    if (not isinstance(credential, dict)
            or set(credential) != {"format_version", "role", "database_instance_id", "deployment_id", "password"}
            or type(credential["format_version"]) is not int or credential["format_version"] != 1):
        raise V1AuthorityUnavailable()
    instance = credential["database_instance_id"]
    try:
        canonical_instance = isinstance(instance, str) and str(uuid.UUID(instance)) == instance
    except ValueError:
        canonical_instance = False
    if (credential["role"] != role or not canonical_instance or instance != str(database_instance_id)
            or type(credential["deployment_id"]) is not str or not deployment_id
            or credential["deployment_id"] != deployment_id):
        raise V1AuthorityUnavailable()
    password = credential["password"]
    if type(password) is not str or len(password) != 43:
        raise V1AuthorityUnavailable()
    try:
        decoded = base64.urlsafe_b64decode(password + "=")
    except ValueError:
        raise V1AuthorityUnavailable() from None
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != password:
        raise V1AuthorityUnavailable()
    if any(password == other for other in forbidden_passwords if other):
        raise V1AuthorityUnavailable()
    return password


def verify_service_boundary(session, service, executor):
    """Inside a transaction connected as S: S's attributes, memberships, rights and the exact executable
    authority inventory. Raises V1AuthorityUnavailable on any difference."""
    from sqlalchemy import text

    def rows(sql, **params):
        return session.execute(text(sql), params).all()

    attributes = rows("""SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolreplication,
        rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = current_user""")
    if [tuple(row) for row in attributes] != [(True, False, False, False, False, False, False)]:
        raise V1AuthorityUnavailable()
    # No membership in any role, of any kind (inherited, SET-only or admin), direct or multi-hop.
    if rows("""SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname <> current_user
        AND (pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER') OR pg_catalog.pg_has_role(current_user, r.oid, 'SET'))"""):
        raise V1AuthorityUnavailable()
    if rows("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = 'kalillac' "
            "AND pg_catalog.has_schema_privilege(current_user, oid, 'CREATE')"):
        raise V1AuthorityUnavailable()
    # No right on any Kalillac relation or column, including PUBLIC's and inherited ones.
    if rows("""SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) r
        WHERE n.nspname = 'kalillac' AND c.relkind IN ('r','p','v','m','f')
        AND (pg_catalog.has_table_privilege(current_user, c.oid, r)
             OR (r IN ('SELECT','INSERT','UPDATE','REFERENCES')
                 AND pg_catalog.has_any_column_privilege(current_user, c.oid, r)))"""):
        raise V1AuthorityUnavailable()
    if rows("""SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'kalillac' AND c.relkind = 'S'
        AND (pg_catalog.has_sequence_privilege(current_user, c.oid, 'USAGE')
             OR pg_catalog.has_sequence_privilege(current_user, c.oid, 'UPDATE'))"""):
        raise V1AuthorityUnavailable()
    # Exactly the reviewed functions in the Kalillac schema, and no SECURITY DEFINER function elsewhere.
    executable = {row[0] for row in rows("""SELECT p.oid::pg_catalog.regprocedure::text FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'kalillac' AND pg_catalog.has_function_privilege(current_user, p.oid, 'EXECUTE')""")}
    reviewed = _reviewed_functions()
    if executable != set(reviewed):
        raise V1AuthorityUnavailable()
    if rows("""SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE p.prosecdef AND n.nspname NOT IN ('kalillac', 'pg_catalog', 'information_schema')
        AND pg_catalog.has_function_privilege(current_user, p.oid, 'EXECUTE')"""):
        raise V1AuthorityUnavailable()
    for signature, digest in reviewed.items():
        found = rows("""SELECT p.prosecdef, p.proconfig, p.prosrc, pg_catalog.pg_get_userbyid(p.proowner),
            ARRAY(SELECT ARRAY[pg_catalog.pg_get_userbyid(a.grantee), a.privilege_type, a.is_grantable::text]
                  FROM pg_catalog.aclexplode(p.proacl) a ORDER BY 1)
            FROM pg_catalog.pg_proc p WHERE p.oid = pg_catalog.to_regprocedure(:f)""", f=signature)
        if len(found) != 1:
            raise V1AuthorityUnavailable()
        definer, config, source, owner, acl = found[0]
        if (definer is not True or list(config or []) != ["search_path=pg_catalog, pg_temp"] or owner != executor
                or hashlib.sha256(source.encode()).hexdigest() != digest
                or sorted(acl) != sorted([[executor, "EXECUTE", "false"], [service, "EXECUTE", "false"]])):
            raise V1AuthorityUnavailable()


class V1Authority:
    """Calls to the V1 authorities as S. Use build_v1_authority(); tests may pass a factory directly."""

    def __init__(self, factory, *, role, executor, production=True):
        self.__factory = factory
        self.role, self.executor, self.production = role, executor, production

    def __repr__(self):
        return "V1Authority()"

    @contextmanager
    def _transaction(self):
        from sqlalchemy import text
        session = self.__factory()
        connection = None
        try:
            with session.begin():
                connection = session.connection()
                if session.get_bind().dialect.name != "postgresql":
                    raise V1AuthorityUnavailable()
                session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                who = session.execute(text("SELECT session_user, current_user, system_user")).one()
                if (who[0] != self.role or who[1] != self.role
                        or (self.production and who[2] != "scram-sha-256:" + self.role)):
                    raise V1AuthorityUnavailable()
                session.execute(text("SET LOCAL lock_timeout = '" + LOCK_TIMEOUT + "'"))
                session.execute(text("SET LOCAL statement_timeout = '" + STATEMENT_TIMEOUT + "'"))
                session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '10s'"))
                yield session
        except Exception:
            # Never return a connection in an unknown protocol state to the pool.
            if connection is not None:
                try:
                    connection.invalidate()
                except Exception:
                    pass
            raise V1AuthorityUnavailable() from None
        finally:
            session.close()

    def verify(self):
        """Re-check S's boundary through this authority's own connection (startup and tests)."""
        with self._transaction() as session:
            verify_service_boundary(session, self.role, self.executor)

    def issue_session(self, *, session_id, token_hash, user_id, verified_password_hash, lifetime_days):
        """One session for a user whose stored password hash the caller has just verified."""
        from sqlalchemy import text
        if (type(session_id) is not uuid.UUID or type(user_id) is not uuid.UUID
                or type(token_hash) is not str or not _TOKEN_HASH.fullmatch(token_hash)
                or type(verified_password_hash) is not str or not verified_password_hash
                or type(lifetime_days) is not int or not 1 <= lifetime_days <= MAX_LIFETIME_DAYS):
            raise V1AuthorityUnavailable()
        with self._transaction() as session:
            found = session.execute(text(_ISSUE), {"s": session_id, "t": token_hash, "u": user_id,
                                                   "h": verified_password_hash, "d": lifetime_days}).all()
            if len(found) != 1:
                raise V1AuthorityUnavailable()
            outcome, issued_at, valid_until, replayed = found[0]
            if outcome not in ISSUE_OUTCOMES or type(replayed) is not bool:
                raise V1AuthorityUnavailable()
            if outcome == "issued":
                if (not isinstance(issued_at, datetime) or not isinstance(valid_until, datetime)
                        or issued_at.tzinfo is None or valid_until - issued_at != timedelta(days=lifetime_days)):
                    raise V1AuthorityUnavailable()
            elif issued_at is not None or valid_until is not None or replayed:
                raise V1AuthorityUnavailable()
            return SessionIssued(outcome, issued_at, valid_until, replayed)

    def end_session(self, *, session_id, token_hash):
        """Sign out: revoke exactly the presented session. Always 'ended' for a well-formed call,
        whether or not anything matched (idempotent, and it reveals nothing)."""
        from sqlalchemy import text
        if type(session_id) is not uuid.UUID or type(token_hash) is not str or not _TOKEN_HASH.fullmatch(token_hash):
            raise V1AuthorityUnavailable()
        with self._transaction() as session:
            found = session.execute(text(_END), {"s": session_id, "t": token_hash}).all()
            if len(found) != 1 or found[0][0] != "ended":
                raise V1AuthorityUnavailable()
            return "ended"

    def reset_password(self, *, operation_id, token_hash, new_password_hash):
        """Complete recovery: the digest of a reset token and the Argon2id hash the caller computed from
        the new password (never the password). Replaces the hash, consumes the account's open reset
        tokens, revokes every session and advances the epoch, once per operation id."""
        from sqlalchemy import text
        if (type(operation_id) is not uuid.UUID or type(token_hash) is not str
                or not _TOKEN_HASH.fullmatch(token_hash) or type(new_password_hash) is not str
                or len(new_password_hash) > 255 or not _ARGON2ID.fullmatch(new_password_hash)):
            raise V1AuthorityUnavailable()
        with self._transaction() as session:
            found = session.execute(text(_RESET), {"o": operation_id, "t": token_hash,
                                                   "h": new_password_hash}).all()
            if len(found) != 1:
                raise V1AuthorityUnavailable()
            outcome, retry_after, replayed = found[0]
            if outcome not in RESET_OUTCOMES or type(replayed) is not bool:
                raise V1AuthorityUnavailable()
            if outcome == "too_many_operations":
                if type(retry_after) is not int or retry_after < 1:
                    raise V1AuthorityUnavailable()
            elif retry_after is not None:
                raise V1AuthorityUnavailable()
            if replayed and outcome != "reset":
                raise V1AuthorityUnavailable()
            return PasswordReset(outcome, retry_after, replayed)

    def change_password(self, *, operation_id, session_id, token_hash, verified_password_hash, new_password_hash):
        """A signed-in customer's password change, proven by the exact original session and the stored hash
        the caller has just verified the current password against. Every other session ends; this one
        stays. The new password itself never reaches this module, only its Argon2id hash."""
        from sqlalchemy import text
        if (type(operation_id) is not uuid.UUID or type(session_id) is not uuid.UUID
                or type(token_hash) is not str or not _TOKEN_HASH.fullmatch(token_hash)
                or type(verified_password_hash) is not str or not verified_password_hash
                or len(verified_password_hash) > 255 or type(new_password_hash) is not str
                or len(new_password_hash) > 255 or not _ARGON2ID.fullmatch(new_password_hash)):
            raise V1AuthorityUnavailable()
        with self._transaction() as session:
            found = session.execute(text(_CHANGE), {"o": operation_id, "s": session_id, "t": token_hash,
                                                    "v": verified_password_hash, "h": new_password_hash}).all()
            if len(found) != 1:
                raise V1AuthorityUnavailable()
            outcome, retry_after, replayed = found[0]
            if outcome not in CHANGE_OUTCOMES or type(replayed) is not bool:
                raise V1AuthorityUnavailable()
            if outcome == "too_many_operations":
                if type(retry_after) is not int or retry_after < 1:
                    raise V1AuthorityUnavailable()
            elif retry_after is not None:
                raise V1AuthorityUnavailable()
            if replayed and outcome != "changed":
                raise V1AuthorityUnavailable()
            return PasswordChanged(outcome, retry_after, replayed)

    def revoke_sessions(self, *, operation_id, session_id, token_hash, scope):
        """Revoke the account's other sessions, or all of them, proven by the exact original session."""
        from sqlalchemy import text
        if (type(operation_id) is not uuid.UUID or type(session_id) is not uuid.UUID
                or type(token_hash) is not str or not _TOKEN_HASH.fullmatch(token_hash)
                or scope not in REVOKE_SCOPES):
            raise V1AuthorityUnavailable()
        with self._transaction() as session:
            found = session.execute(text(_REVOKE), {"o": operation_id, "s": session_id, "t": token_hash,
                                                    "c": scope}).all()
            if len(found) != 1:
                raise V1AuthorityUnavailable()
            outcome, retry_after, replayed = found[0]
            if outcome not in REVOKE_OUTCOMES or type(replayed) is not bool:
                raise V1AuthorityUnavailable()
            if outcome == "too_many_operations":
                if type(retry_after) is not int or retry_after < 1:
                    raise V1AuthorityUnavailable()
            elif retry_after is not None:
                raise V1AuthorityUnavailable()
            if replayed and outcome != "revoked":
                raise V1AuthorityUnavailable()
            return SessionsRevoked(outcome, retry_after, replayed)


def build_v1_authority(ordinary_url, *, role, executor, credential, database_instance_id, deployment_id,
                       ordinary_password=None, production=True):
    """The authority over its own small pool, logged in as S with the protected credential's password,
    after verifying S's boundary through that pool. Raises V1AuthorityUnavailable; the engine is
    disposed on any failure. `credential` is the credential file's bytes."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from kalillac_db.migration_roles import validate_role_name
    engine = None
    try:
        role = validate_role_name(role, "V1 service role")
        executor = validate_role_name(executor, "V1 executor role")
        if role == executor or ordinary_url.username in (role, executor):
            raise V1AuthorityUnavailable()
        password = parse_service_credential(credential, role=role, database_instance_id=database_instance_id,
                                            deployment_id=deployment_id,
                                            forbidden_passwords=(ordinary_password, ordinary_url.password))
        engine = create_engine(
            ordinary_url.set(username=role, password=password), pool_size=POOL_SIZE, max_overflow=0,
            pool_timeout=POOL_TIMEOUT_SECONDS, pool_pre_ping=True, hide_parameters=True,
            isolation_level="READ COMMITTED",
            connect_args={"connect_timeout": 5, "application_name": "kalillac-v1-authority",
                          "options": "-c statement_timeout=5000 -c lock_timeout=3000"})

        @event.listens_for(engine, "checkout")
        def _identity(connection, record, proxy):
            with connection.cursor() as cursor:
                cursor.execute("SELECT session_user, current_user, system_user")
                found = cursor.fetchone()
            connection.rollback()
            if found[0] != role or found[1] != role or (production and found[2] != "scram-sha-256:" + role):
                raise V1AuthorityUnavailable()

        authority = V1Authority(sessionmaker(bind=engine, expire_on_commit=False), role=role, executor=executor,
                                production=production)
        authority.verify()
        authority.dispose = engine.dispose
        return authority
    except Exception:
        if engine is not None:
            engine.dispose()
        raise V1AuthorityUnavailable() from None


def load_v1_authority(ordinary_factory, environ, *, read_file=None, production=True):
    """build_v1_authority from the deployment's settings: the credential file named by
    KALILLAC_V1_SERVICE_CREDENTIALS_FILE (read through the strict protected-file loader), the roles'
    settings, KALILLAC_DEPLOYMENT_ID, and the database instance recorded by registration activation,
    read through the runtime login, whose password the S password must differ from."""
    from sqlalchemy import text
    from kalillac_db.migration_roles import v1_security_executor_role, v1_security_service_role
    try:
        if read_file is None:
            from .registration_files import read_protected as read_file
        factory = ordinary_factory()
        if factory is None:
            raise V1AuthorityUnavailable()
        with factory() as session, session.begin():
            ordinary_url = session.get_bind().url
            instance = session.execute(text(
                "SELECT database_instance_id FROM kalillac.account_registration_activation WHERE singleton_id = 1")).scalar_one()
        if instance is None:
            raise V1AuthorityUnavailable()
        credential = read_file(environ.get(CREDENTIAL_FILE_ENV, ""), CREDENTIAL_MAX_BYTES)
        return build_v1_authority(ordinary_url, role=v1_security_service_role(), executor=v1_security_executor_role(),
                                  credential=credential, database_instance_id=instance,
                                  deployment_id=environ.get("KALILLAC_DEPLOYMENT_ID", ""), production=production)
    except Exception:
        raise V1AuthorityUnavailable() from None
