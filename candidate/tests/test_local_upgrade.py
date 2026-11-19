"""Local upgrade: real disposable PostgreSQL and filesystem copies, never the installed root."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import uuid

import pytest
pytest.importorskip("pwd")
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy/local"))
import upgrade_support as u
import rehearse_upgrade as rehearsal
import live_upgrade as live_tool
from test_v1_transition_postgres import world, run as activate  # noqa: F401
from test_v1_issuance_postgres import account


@pytest.fixture(scope="module")
def pg():
    require_root = os.geteuid() == 0
    if not require_root:
        pytest.skip("run this isolated cluster suite as root (initdb runs as nobody)")
    bindir = os.environ.get("KALILLAC_LOCAL_PG_BIN", "/usr/lib/postgresql/16/bin")
    if not Path(bindir, "initdb").exists():
        pytest.fail("explicit PostgreSQL 16 binaries required")
    base = Path(tempfile.mkdtemp(prefix="ku-pg-"))
    os.chmod(base, 0o711)
    data, sock = base / "pg", base / "sock"
    for path in (data, sock):
        path.mkdir(mode=0o700)
        os.chown(path, 65534, 65534)
    cluster = u.Cluster(data, bindir, 65534, 65534)
    password = base / "password"
    password.write_text("synthetic-upgrade-only\n")
    os.chmod(password, 0o644)
    result = cluster.command("-D", str(data), "-U", "postgres", f"--pwfile={password}",
                             "--auth-local=trust", "--auth-host=scram-sha-256", "-E", "UTF8", tool="initdb")
    assert result.returncode == 0, result.stderr
    port = rehearsal.free_port()
    cluster.start(sock, port)
    yield SimpleNamespace(cluster=cluster, base=base, socket=sock, port=port, bin=bindir)
    cluster.stop()
    shutil.rmtree(base)


@pytest.fixture(autouse=True)
def disposable_database(pg, monkeypatch):
    import registration_test_support
    # Cold-copy tests restart only their own source cluster; its pooled sessions must reconnect.
    monkeypatch.setattr(registration_test_support, "create_engine",
                        lambda *a, **kw: create_engine(*a, pool_pre_ping=True, **kw))
    monkeypatch.setenv("KALILLAC_REGISTRATION_TEST_POSTGRES_URL",
                       f"postgresql+psycopg://postgres:synthetic-upgrade-only@127.0.0.1:{pg.port}/postgres")
    monkeypatch.setenv("KALILLAC_REGISTRATION_REQUIRE_POSTGRES", "true")


@pytest.fixture
def prepared(world, pg, tmp_path, monkeypatch):
    w = world
    w.admin_engine.pool._pre_ping = True
    assert activate(w, "activate", "--release-sha", "a" * 40, "--apply") == 0
    user, _hash = account(w)
    from kalillac_db.models import Space, Conversation, Message, AccountEntitlement, AccountUsageDaily, AccountBilling
    from datetime import datetime, timezone
    with Session(w.admin_engine) as session, session.begin():
        space = Space(user_id=user, name="Saved space", instructions="Original instructions")
        session.add(space)
        session.flush()
        conversation = Conversation(space_id=space.id, user_id=user, title="Saved conversation", message_count=1)
        session.add(conversation)
        session.flush()
        session.add(Message(conversation_id=conversation.id, user_id=user, seq=1, role="user",
                            content="Preserve this exact text", client_request_id=uuid.uuid4()))
        session.add(AccountEntitlement(user_id=user, tier="paid", source="synthetic-upgrade"))
        session.add(AccountBilling(user_id=user, stripe_customer_id="cus_synthetic_upgrade",
                                   stripe_subscription_id="sub_synthetic_upgrade", subscription_status="active",
                                   stripe_price_id="price_synthetic_upgrade"))
        session.add(AccountUsageDaily(user_id=user, usage_date=datetime.now(timezone.utc).date(),
                                      successful_chats=7, request_chars=19, response_chars=23))
    with w.admin_engine.begin() as c:
        c.execute(text("UPDATE kalillac.account_security_activation SET deployment_id=:d"), {"d": u.DEPLOYMENT})
    deployment = {"KALILLAC_DEPLOYMENT_ID": u.DEPLOYMENT, "KALILLAC_DB_NAME": w.migration.url.database,
                  "KALILLAC_MIGRATION_OWNER_ROLE": w.names["owner"], "KALILLAC_MIGRATION_APP_ROLE": w.names["app"],
                  "KALILLAC_MIGRATION_REGISTRATION_ROLE": w.registration_role}
    u.migrate(w.migration, deployment, u.OLD)
    before = u.database_snapshot(w.admin_engine)
    root = pg.base / ("fixture-" + uuid.uuid4().hex[:6])
    root.mkdir(mode=0o755)
    for folder in ("admin", "deployment/private", "state/files", "secrets", "run", "pgsock"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    os.chown(root / "pgsock", 65534, 65534)
    (root / "admin/admin-url").write_text(w.admin_engine.url.set(database="postgres").render_as_string(hide_password=False))
    (root / "deployment/deployment.env").write_text("".join(f"{k}={v}\n" for k,v in deployment.items()))
    (root / "deployment/private/migration-login.json").write_text(json.dumps(
        {"role": w.migration.url.username, "password": w.migration.url.password}))
    (root / "settings.env").write_text(f"KALILLAC_RELEASE_SHA={'a'*40}\nKALILLAC_STRIPE_MODE=test\n"
                                       "KALILLAC_SITE_ORIGIN=http://localhost:8010\n")
    (root / "local.json").write_text(json.dumps({"deployment": u.DEPLOYMENT, "release_sha": "a"*40,
                                                "stripe": "simulated"}))
    from kalillac_files.store import FileStore
    record = FileStore(root / "state/files").put(str(user), b"Original stored document", "saved.txt", "text/plain", "uploaded")
    # Only our isolated source cluster is stopped/copied. Never the installed one.
    w.admin_engine.dispose()
    w.migration.dispose()
    pg.cluster.stop()
    try:
        u.copy_tree(pg.cluster.pg, root / "pg")
        backup = pg.base / ("backup-" + uuid.uuid4().hex[:6])
        checksum = live_tool.backup_install(root, backup, "a"*40, pg.bin, 65534, 65534)
    finally:
        pg.cluster.start(pg.socket, pg.port)
    yield SimpleNamespace(root=root, backup=backup, checksum=checksum, before=before, record=record,
                          deployment=deployment, bindir=pg.bin)
    # Both the original application test DB and its pool remain at 0025; restore for shared fixture teardown.
    u.migrate(w.migration, deployment)
    shutil.rmtree(root)
    shutil.rmtree(backup)


def test_complete_rehearsal_and_same_count_tampering(prepared, tmp_path):
    p = prepared
    receipt = tmp_path / "receipt.json"
    result = rehearsal.rehearse(p.backup, p.checksum, p.bindir, receipt)
    u.same_contents(p.before, result["database"])
    assert result["files"]["records"] == result["files"]["accessible"] == 1
    assert result["upgraded_to"] == u.NEW and result["restored_to"] == u.OLD
    assert u.validate_backup(p.backup, p.checksum)
    port = rehearsal.free_port()
    with u.copied_cluster(p.backup, p.checksum, p.bindir, port=port) as (root, manifest, cluster):
        admin, migration = u.engines(root, port)
        try:
            for table, column, value in (
                ("users", "password_hash", "changed synthetic hash"), ("spaces", "instructions", "changed"),
                ("conversations", "title", "changed"), ("messages", "content", "changed"),
                ("account_entitlements", "tier", "free"),
                ("account_billing", "subscription_status", "canceled"),
                ("account_security_activation", "release_sha", "b"*40)):
                with admin.begin() as c:
                    original = c.scalar(text(f'SELECT "{column}" FROM kalillac."{table}" LIMIT 1'))
                    c.exec_driver_sql("SET LOCAL session_replication_role=replica")
                    c.execute(text(f'UPDATE kalillac."{table}" SET "{column}"=:v'), {"v":value})
                try:
                    with pytest.raises(u.Refused, match="contents"):
                        u.same_contents(p.before, u.database_snapshot(admin))
                finally:
                    with admin.begin() as c:
                        c.exec_driver_sql("SET LOCAL session_replication_role=replica")
                        c.execute(text(f'UPDATE kalillac."{table}" SET "{column}"=:v'), {"v":original})
                u.same_contents(p.before, u.database_snapshot(admin))
            blob = next((root / "state/files").glob("*/*.bin"))
            data = blob.read_bytes()
            blob.write_bytes(bytes([data[0] ^ 1]) + data[1:])
            assert u.tree_manifest(root / "state/files") != result["files"]["manifest"]
        finally:
            admin.dispose()
            migration.dispose()


@pytest.mark.parametrize("failure", ["before", "partial", "committed", "startup"])
def test_failed_upgrade_restores_from_cold_backup(prepared, failure):
    p = prepared
    port = rehearsal.free_port()
    with pytest.raises(RuntimeError, match="injected"):
        with u.copied_cluster(p.backup, p.checksum, p.bindir, port=port) as (root, manifest, cluster):
            admin, migration = u.engines(root, port)
            try:
                if failure == "before":
                    raise RuntimeError("injected before migration")
                if failure == "partial":
                    def interrupt(c):
                        c.execute(text("UPDATE kalillac.messages SET content='interrupted'"))
                        raise RuntimeError("injected partial migration")
                    try:
                        u.migrate(migration, p.deployment, after_sql=interrupt)
                    except RuntimeError:
                        u.same_contents(p.before, u.database_snapshot(admin))
                        raise
                u.migrate(migration, p.deployment)
                if failure == "committed":
                    with admin.begin() as c:
                        c.execute(text("UPDATE kalillac.messages SET content='committed bad work'"))
                raise RuntimeError("injected startup/committed failure")
            finally:
                admin.dispose()
                migration.dispose()
    port = rehearsal.free_port()
    with u.copied_cluster(p.backup, p.checksum, p.bindir, port=port) as (root, manifest, cluster):
        admin, migration = u.engines(root, port)
        try:
            u.same_contents(p.before, u.database_snapshot(admin))
            assert rehearsal.stored_files(root, admin)["accessible"] == 1
            u.database_identity(admin, cluster.pg, p.deployment["KALILLAC_DB_NAME"], "a"*40)
        finally:
            admin.dispose()
            migration.dispose()


@pytest.mark.parametrize("code", [1, 2, 4, 127])
def test_unknown_status_never_confirms_stop(pg, monkeypatch, code):
    cluster = u.Cluster("/unused", pg.bin, 65534, 65534)
    monkeypatch.setattr(cluster, "command", lambda *a, **k: subprocess.CompletedProcess(a, code, "", ""))
    with pytest.raises(u.Refused, match="unknown"):
        cluster.stop()


def test_failed_shutdown_preserves_copy(prepared, monkeypatch):
    p = prepared
    captured = []
    original = u.Cluster.stop
    def fail_only_copy(cluster):
        if "kalillac-upgrade-copy-" in str(cluster.pg):
            captured.append(cluster)
            raise u.Refused("injected shutdown failure")
        original(cluster)
    with monkeypatch.context() as patch:
        patch.setattr(u.Cluster, "stop", fail_only_copy)
        with pytest.raises(u.Refused, match="shutdown"):
            with u.copied_cluster(p.backup, p.checksum, p.bindir, port=rehearsal.free_port()):
                pass
    assert captured[0].pg.exists() and captured[0].status() == 0
    original(captured[0])
    assert captured[0].pg.exists()


@pytest.mark.parametrize("corruption", ["blob", "metadata", "checksum"])
def test_invalid_backup_is_refused_before_start(prepared, corruption):
    p = prepared
    if corruption == "checksum":
        with pytest.raises(u.Refused, match="checksum"):
            u.validate_backup(p.backup, "0"*64)
    else:
        target = next((p.backup / "payload/state/files").glob("*/*." + ("bin" if corruption == "blob" else "json")))
        data = target.read_bytes()
        target.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        with pytest.raises(u.Refused, match="contents"):
            u.validate_backup(p.backup, p.checksum)


def test_live_migration_only_on_disposable_fixture(prepared, pg, tmp_path):
    p = prepared
    receipt = tmp_path / "verified-rehearsal.json"
    result = rehearsal.rehearse(p.backup, p.checksum, p.bindir, receipt)
    # The conservative service-uid guard also sees our source test cluster; stop only that fixture.
    pg.cluster.stop()
    port = rehearsal.free_port()
    try:
        live_tool.apply_upgrade(p.root, p.backup, p.checksum, receipt, u.digest(result), "a"*40,
                                p.bindir, 65534, 65534, u.RELEASE, port=port)
        cluster = u.Cluster(p.root / "pg", p.bindir, 65534, 65534)
        cluster.stopped()
        cluster.start(p.root / "pgsock", port)
        admin, migration = u.engines(p.root, port)
        try:
            u.same_contents(p.before, u.database_snapshot(admin, project_new=True))
            assert rehearsal.stored_files(p.root, admin) == result["files"]
        finally:
            admin.dispose()
            migration.dispose()
            cluster.stop()
        settings = u.env_file(p.root / "settings.env")
        assert settings["KALILLAC_RELEASE_SHA"] == "a"*40
        assert all(settings[k] == v for k,v in live_tool.SYNTHETIC.items())
        u.validate_backup(p.backup, p.checksum)
    finally:
        pg.cluster.start(pg.socket, pg.port)


def test_wrong_destination_release_and_authorization(tmp_path):
    with pytest.raises(u.Refused):
        live_tool.destination(tmp_path, 0, 0, "a"*40, tmp_path, u.RELEASE)
    with pytest.raises(u.Refused):
        live_tool.checkout_identity(tmp_path, "b"*40)
    assert live_tool.main(["backup", "--root", str(tmp_path), "--device", "0", "--inode", "0",
        "--activation-release", "a"*40, "--checkout", str(tmp_path), "--checkout-sha", u.RELEASE,
        "--pg-bin", "/unused", "--backup", str(tmp_path/"backup")]) == 2


def test_checks_survive_python_optimization():
    result = subprocess.run([sys.executable, "-O", "-c",
        f"import sys;sys.path.insert(0,{str(Path(u.__file__).parent)!r});"
        "import upgrade_support as u;u.require(False,'integrity failure')"], capture_output=True, text=True)
    assert result.returncode != 0 and "Refused" in result.stderr
