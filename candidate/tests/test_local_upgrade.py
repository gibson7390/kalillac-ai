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
    # As on a real installation, the application (the service identity, here 65534) owns what it wrote.
    for folder, _dirs, names in os.walk(root / "state"):
        for path in (folder, *(os.path.join(folder, n) for n in names)):
            os.chown(path, 65534, 65534)
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


def test_stop_uses_verified_destination_not_ambient_settings(tmp_path, monkeypatch):
    import local_staging
    root, checkout, bindir = tmp_path / "verified-root", tmp_path / "checkout", tmp_path / "pg-bin"
    monkeypatch.setenv("KALILLAC_LOCAL_ROOT", "/different/installation")
    monkeypatch.setenv("KALILLAC_LOCAL_PG_BIN", "/different/postgres")
    prior = (local_staging.CANDIDATE, local_staging.REPO, local_staging.ROOT)
    observed = []
    def checked_stop(argv):
        observed.append((argv, os.environ["KALILLAC_LOCAL_ROOT"], os.environ["KALILLAC_LOCAL_PG_BIN"],
                         local_staging.CANDIDATE, local_staging.REPO))
        return 0
    monkeypatch.setattr(local_staging, "main", checked_stop)
    live_tool.stop_install(root, checkout, bindir)
    assert observed == [(["stop"], str(root), str(bindir), checkout / "candidate", checkout)]
    assert os.environ["KALILLAC_LOCAL_ROOT"] == "/different/installation"
    assert os.environ["KALILLAC_LOCAL_PG_BIN"] == "/different/postgres"
    assert (local_staging.CANDIDATE, local_staging.REPO, local_staging.ROOT) == prior


# --- Codex's audit of ca92dc6: U1 to U4 and the recovery rehearsal, as permanent regressions -----------------

import stat  # noqa: E402

SERVICE = 65534


def fake_payload(tmp_path, admin="postgresql+psycopg://postgres:synthetic@127.0.0.1:5432/postgres",
                 login=None):
    payload = tmp_path / "payload"
    (payload / "admin").mkdir(parents=True)
    (payload / "deployment/private").mkdir(parents=True)
    (payload / "admin/admin-url").write_text(admin)
    (payload / "deployment/deployment.env").write_text("KALILLAC_DB_NAME=kalillac_local\n")
    (payload / "deployment/private/migration-login.json").write_text(json.dumps(
        login if login is not None else {"role": "kalillac_local_migrate", "password": "synthetic"}))
    return payload


def no_engine(monkeypatch):
    created = []
    monkeypatch.setattr(u, "create_engine", lambda *a, **k: created.append(a) or pytest.fail("engine created"))
    return created


# U1: nothing in a copied DSN or the environment can choose another destination, and the refusal comes before
# any engine exists.

@pytest.mark.parametrize("option", ["host", "hostaddr", "port", "dbname", "service", "options", "user", "password",
                                    "target_session_attrs", "sslmode", "passfile"])
def test_u1_a_query_parameter_in_the_admin_url_is_refused_before_any_engine(tmp_path, monkeypatch, option):
    created = no_engine(monkeypatch)
    payload = fake_payload(tmp_path, f"postgresql+psycopg://postgres:synthetic@127.0.0.1:5432/postgres?{option}=x")
    with pytest.raises(u.Refused, match="connection options"):
        u.engines(payload, 55999)
    assert created == []


@pytest.mark.parametrize("variable", u.DESTINATION_ENVIRONMENT)
def test_u1_a_libpq_environment_setting_is_refused_before_any_engine(tmp_path, monkeypatch, variable):
    created = no_engine(monkeypatch)
    monkeypatch.setenv(variable, "127.0.0.1" if "HOST" in variable else "41109")
    with pytest.raises(u.Refused, match="environment"):
        u.engines(fake_payload(tmp_path), 55999)
    assert created == []


@pytest.mark.parametrize("login", [{"role": "x", "password": "p", "host": "elsewhere"}, {"role": "a b", "password": "p"},
                                   {"role": "kalillac_local_migrate"}, {"role": "kalillac_local_migrate", "password": ""}])
def test_u1_a_malformed_migration_login_is_refused_before_any_engine(tmp_path, monkeypatch, login):
    created = no_engine(monkeypatch)
    with pytest.raises(u.Refused, match="migration login"):
        u.engines(fake_payload(tmp_path, login=login), 55999)
    assert created == []


@pytest.mark.parametrize("admin", ["postgresql+psycopg://postgres:s@db.example.test:5432/postgres",
                                   "postgresql+psycopg2://postgres:s@127.0.0.1:5432/postgres",
                                   "postgresql+psycopg://postgres@127.0.0.1:5432/postgres",
                                   "postgresql+psycopg://postgres:s@127.0.0.1:5432/other"])
def test_u1_a_foreign_driver_host_database_or_missing_password_is_refused(tmp_path, monkeypatch, admin):
    created = no_engine(monkeypatch)
    with pytest.raises(u.Refused):
        u.engines(fake_payload(tmp_path, admin), 55999)
    assert created == []


def test_u1_both_engines_carry_exactly_the_verified_destination(tmp_path):
    admin, migration = u.engines(fake_payload(tmp_path), 55999)
    try:
        for engine, user in ((admin, "postgres"), (migration, "kalillac_local_migrate")):
            _args, params = engine.dialect.create_connect_args(engine.url)
            params = {k: v for k, v in params.items() if k != "context"}
            assert params == {"host": "127.0.0.1", "port": 55999, "dbname": "kalillac_local", "user": user,
                              "password": "synthetic"}
            assert not engine.url.query
    finally:
        admin.dispose()
        migration.dispose()


def test_u1_codex_redirect_through_query_parameters_is_refused_on_real_clusters(prepared, pg, monkeypatch):
    """Codex's reproduction: host and port in the admin URL's query pointed the scratch connection at the
    running source fixture. Now the URL is refused, and no engine (so no connection) is made."""
    from sqlalchemy import make_url
    p = prepared
    path = p.root / "admin/admin-url"
    original = make_url(path.read_text().strip())
    path.write_text(original.update_query_dict({"host": "127.0.0.1", "port": str(pg.port)})
                    .render_as_string(hide_password=False))
    created = no_engine(monkeypatch)
    with pytest.raises(u.Refused, match="connection options"):
        u.engines(p.root, rehearsal.free_port())
    assert created == []


# U2: the checkout check works for every supported layout and refuses everything else, read-only.

def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git("init", "-q", "-b", "trunk", cwd=main)
    (main / "tracked.txt").write_text("synthetic\n")
    git("add", ".", cwd=main)
    git("commit", "-q", "-m", "synthetic", cwd=main)
    sha = git("rev-parse", "HEAD", cwd=main)
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", "--detach", str(linked), cwd=main)
    return SimpleNamespace(main=main, linked=linked, sha=sha)


def test_u2_a_repository_and_a_linux_linked_worktree_are_accepted(repository):
    r = repository
    live_tool.checkout_identity(r.main, r.sha, branch="trunk")
    live_tool.checkout_identity(r.linked, r.sha)
    with pytest.raises(u.Refused, match="branch"):
        live_tool.checkout_identity(r.linked, r.sha, branch="trunk")         # Detached: not the required branch.


@pytest.mark.parametrize("change", ["tracked", "untracked", "staged", "sha", "subdirectory", "not-git"])
def test_u2_dirty_wrong_or_misplaced_checkouts_are_refused_and_left_alone(repository, change):
    r = repository
    target, expected = r.linked, r.sha
    if change == "tracked":
        (target / "tracked.txt").write_text("edited\n")
    elif change == "untracked":
        (target / "new.txt").write_text("untracked\n")
    elif change == "staged":
        (target / "staged.txt").write_text("staged\n")
        git("add", "staged.txt", cwd=target)
    elif change == "sha":
        expected = "0" * 40
    elif change == "subdirectory":
        (target / "sub").mkdir()
        (target / "sub/keep.txt").write_text("x\n")
        git("add", ".", cwd=target)
        git("commit", "-q", "-m", "sub", cwd=target)
        expected = git("rev-parse", "HEAD", cwd=target)
        target = target / "sub"
    else:
        target = target.parent / "plain"
        target.mkdir()
    before = {p: p.read_bytes() for p in (r.linked).rglob("*") if p.is_file() and ".git" not in p.parts}
    with pytest.raises(u.Refused):
        live_tool.checkout_identity(target, expected)
    assert {p: p.read_bytes() for p in (r.linked).rglob("*") if p.is_file() and ".git" not in p.parts} == before


def test_u2_git_environment_variables_cannot_select_another_checkout(repository, monkeypatch):
    r = repository
    (r.linked / "dirty.txt").write_text("uncommitted\n")
    monkeypatch.setenv("GIT_DIR", str(r.main / ".git"))                     # Clean, at the same commit.
    monkeypatch.setenv("GIT_WORK_TREE", str(r.main))
    monkeypatch.setenv("GIT_INDEX_FILE", str(r.main / ".git/index"))
    with pytest.raises(u.Refused, match="uncommitted"):
        live_tool.checkout_identity(r.linked, r.sha)


def test_u2_a_windows_created_worktree_without_git_for_windows_fails_closed(repository, monkeypatch):
    r = repository
    (r.linked / ".git").write_text("gitdir: C:/Kalillac/somewhere/.git/worktrees/linked\n")
    monkeypatch.setattr(live_tool, "WINDOWS_GIT", ("/nonexistent/git.exe",))
    with pytest.raises(u.Refused, match="Git for Windows"):
        live_tool.checkout_identity(r.linked, r.sha)


# U3 and the runtime files: access is what the service identity can do, never what root can.

def service_can_read(path):
    return subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", "import sys;open(sys.argv[1],'rb').read()",
                           str(path)], user=SERVICE, group=SERVICE, extra_groups=[], capture_output=True).returncode == 0


@pytest.fixture
def stored(prepared, pg):
    """The prepared root's files, with an engine on the source fixture's database (same users)."""
    p = prepared
    admin, migration = u.engines(p.root, pg.port)
    migration.dispose()
    yield p, admin, next((p.root / "state/files").glob("*/*.bin"))
    admin.dispose()


def test_u3_service_readable_files_pass(stored):
    p, admin, _blob = stored
    result = rehearsal.stored_files(p.root, admin, SERVICE, SERVICE)
    assert result["records"] == result["accessible"] == 1


@pytest.mark.parametrize("damage", ["blob", "metadata", "folder", "supplementary-group-only"])
def test_u3_a_file_root_can_read_but_the_service_cannot_is_refused(stored, damage):
    p, admin, blob = stored
    target = {"blob": blob, "metadata": blob.with_suffix(".json"), "folder": blob.parent,
              "supplementary-group-only": blob}[damage]
    if damage == "supplementary-group-only":
        os.chown(target, 0, 12345)            # Readable only through a group the service is not launched with.
        os.chmod(target, 0o640)
    else:
        os.chown(target, 0, 0)
        os.chmod(target, 0o700 if target.is_dir() else 0o600)
    assert target.is_dir() or target.read_bytes()                       # Root can read it...
    assert not service_can_read(blob if damage == "folder" else target)  # ...the service cannot.
    with pytest.raises(u.Refused, match="service identity"):
        rehearsal.stored_files(p.root, admin, SERVICE, SERVICE)


def test_u3_group_access_through_the_service_primary_group_is_accepted(stored):
    p, admin, blob = stored
    os.chown(blob, 0, SERVICE)
    os.chmod(blob, 0o640)
    assert service_can_read(blob)
    assert rehearsal.stored_files(p.root, admin, SERVICE, SERVICE)["accessible"] == 1


def test_u3_codex_scenario_a_service_unreadable_document_fails_the_whole_rehearsal(prepared, pg, tmp_path):
    """Codex's reproduction: a root-owned 0600 blob was certified accessible=1. Now the rehearsal refuses."""
    p = prepared
    blob = next((p.root / "state/files").glob("*/*.bin"))
    os.chown(blob, 0, 0)
    os.chmod(blob, 0o600)
    assert not service_can_read(blob)
    backup = pg.base / ("u3-backup-" + uuid.uuid4().hex[:6])
    pg.cluster.stop()
    try:
        checksum = live_tool.backup_install(p.root, backup, "a" * 40, p.bindir, SERVICE, SERVICE)
    finally:
        pg.cluster.start(pg.socket, pg.port)
    with pytest.raises(u.Refused, match="service identity"):
        rehearsal.rehearse(backup, checksum, p.bindir, tmp_path / "receipt.json")
    assert not (tmp_path / "receipt.json").exists()
    shutil.rmtree(backup)


@pytest.fixture
def runtime_root():
    base = Path(tempfile.mkdtemp(prefix="ku-runtime-"))   # Traversable by the service, unlike pytest's 0700 tmp.
    os.chmod(base, 0o711)
    root = base / "kalillac-local-staging"
    for d in ("deployment/protected", "credentials", "state/files", "state/locks", "state/outbox"):
        (root / d).mkdir(parents=True)
    for d in ("state", "state/files", "state/locks", "state/outbox", "credentials"):
        os.chown(root / d, SERVICE, SERVICE)
    os.chmod(root / "credentials", 0o500)
    keyring = root / "deployment/protected/keyring"
    keyring.write_text("synthetic keyring\n")
    os.chown(keyring, 0, SERVICE)
    os.chmod(keyring, 0o440)                     # root:service-group, as provisioning writes it.
    secret = root / "credentials/kalillac_db_password"
    secret.write_text("synthetic\n")
    os.chown(secret, SERVICE, SERVICE)
    os.chmod(secret, 0o400)
    (root / "deployment/deployment.env").write_text(f"KALILLAC_ACCOUNT_REGISTRATION_KEYRING_FILE={keyring}\n")
    (root / "settings.env").write_text("KALILLAC_RELEASE_SHA=" + "a" * 40 + "\n")
    yield root
    shutil.rmtree(base)


def test_runtime_files_readable_by_the_service_pass(runtime_root):
    assert u.runtime_access(runtime_root, runtime_root, SERVICE, SERVICE) == {"runtime_files": 2,
                                                                                "writable_folders": 4}


@pytest.mark.parametrize("damage", ["credential-root-only", "keyring-other-group", "outside", "missing", "state"])
def test_runtime_files_the_service_cannot_use_are_refused(runtime_root, damage):
    root = runtime_root
    if damage == "credential-root-only":
        os.chown(root / "credentials/kalillac_db_password", 0, 0)
    elif damage == "keyring-other-group":
        os.chown(root / "deployment/protected/keyring", 0, 12345)
    elif damage == "outside":
        (root / "deployment/deployment.env").write_text("KALILLAC_ACCOUNT_REGISTRATION_KEYRING_FILE=/etc/hostname\n")
    elif damage == "missing":
        (root / "deployment/deployment.env").write_text(
            f"KALILLAC_ACCOUNT_REGISTRATION_KEYRING_FILE={root}/deployment/protected/absent\n")
    else:
        os.chown(root / "state/locks", 0, 0)
    with pytest.raises(u.Refused):
        u.runtime_access(root, root, SERVICE, SERVICE)


# U4: the settings file keeps its owner and mode; a link, a second link or extended attributes are refused.

@pytest.mark.parametrize("owner,mode", [((0, 0), 0o600), ((0, SERVICE), 0o640), ((0, 0), 0o644), ((SERVICE, SERVICE), 0o400)])
def test_u4_staged_settings_keep_owner_mode_and_content(tmp_path, owner, mode):
    target = tmp_path / "settings.env"
    target.write_text("KALILLAC_RELEASE_SHA=" + "a" * 40 + "\nOTHER=kept\n")
    os.chown(target, *owner)
    os.chmod(target, mode)
    original = target.read_bytes()
    staged = live_tool.stage_settings(target, live_tool.SETTINGS_ADDITION)
    st = os.stat(staged.path)
    assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (*owner, mode)
    live_tool.commit_settings(staged)
    st = os.stat(target)
    assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (*owner, mode)
    assert target.read_bytes() == original + live_tool.SETTINGS_ADDITION.encode()
    assert not os.path.lexists(staged.path)


@pytest.mark.parametrize("problem", ["symlink", "hardlink", "xattr", "staging-exists", "no-newline"])
def test_u4_unsafe_settings_files_are_refused_and_left_unchanged(tmp_path, problem):
    target = tmp_path / "settings.env"
    real = tmp_path / "real.env"
    real.write_text("KALILLAC_RELEASE_SHA=" + "a" * 40 + "\n")
    os.chmod(real, 0o600)
    if problem == "symlink":
        target.symlink_to(real)
    else:
        os.rename(real, target)
        if problem == "hardlink":
            os.link(target, tmp_path / "second")
        elif problem == "xattr":
            os.setxattr(target, "user.kalillac-test", b"1")
        elif problem == "staging-exists":
            (tmp_path / "settings.env.upgrade-new").write_text("left from before\n")
        else:
            target.write_text("NO_NEWLINE=1")
    before = {p.name: (p.read_bytes(), stat.S_IMODE(os.lstat(p).st_mode)) for p in tmp_path.iterdir()
              if not p.is_symlink()}
    with pytest.raises(u.Refused):
        live_tool.stage_settings(target, live_tool.SETTINGS_ADDITION)
    after = {p.name: (p.read_bytes(), stat.S_IMODE(os.lstat(p).st_mode)) for p in tmp_path.iterdir()
             if not p.is_symlink()}
    assert after == before


def test_u4_settings_changed_after_staging_are_not_replaced(tmp_path):
    target = tmp_path / "settings.env"
    target.write_text("A=1\n")
    os.chmod(target, 0o600)
    staged = live_tool.stage_settings(target, live_tool.SETTINGS_ADDITION)
    replacement = tmp_path / "other"
    replacement.write_text("B=2\n")
    os.replace(replacement, target)                    # Another file now has the name.
    with pytest.raises(u.Refused, match="changed since staging"):
        live_tool.commit_settings(staged)
    assert target.read_text() == "B=2\n" and Path(staged.path).exists()


def test_u4_codex_scenario_a_0600_settings_file_stays_0600_through_apply(prepared, pg, tmp_path):
    p = prepared
    path = p.root / "settings.env"
    os.chmod(path, 0o600)
    backup = pg.base / ("u4-backup-" + uuid.uuid4().hex[:6])
    pg.cluster.stop()
    try:
        checksum = live_tool.backup_install(p.root, backup, "a" * 40, p.bindir, SERVICE, SERVICE)
    finally:
        pg.cluster.start(pg.socket, pg.port)
    receipt = tmp_path / "receipt.json"
    result = rehearsal.rehearse(backup, checksum, p.bindir, receipt)
    original = path.read_bytes()
    pg.cluster.stop()
    try:
        live_tool.apply_upgrade(p.root, backup, checksum, receipt, u.digest(result), "a" * 40,
                                p.bindir, SERVICE, SERVICE, u.RELEASE, port=rehearsal.free_port())
    finally:
        pg.cluster.start(pg.socket, pg.port)
    st = path.stat()
    assert stat.S_IMODE(st.st_mode) == 0o600 and (st.st_uid, st.st_gid) == (0, 0)
    assert path.read_bytes() == original + live_tool.SETTINGS_ADDITION.encode()
    shutil.rmtree(backup)


def test_u4_a_failed_migration_leaves_the_settings_file_exactly_as_it_was(prepared, pg, tmp_path, monkeypatch):
    p = prepared
    receipt = tmp_path / "receipt.json"
    result = rehearsal.rehearse(p.backup, p.checksum, p.bindir, receipt)
    path = p.root / "settings.env"
    before = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_ino)

    def broken(*args, **kwargs):
        raise RuntimeError("injected migration failure")
    monkeypatch.setattr(live_tool, "migrate", broken)
    pg.cluster.stop()
    try:
        with pytest.raises(RuntimeError, match="injected"):
            live_tool.apply_upgrade(p.root, p.backup, p.checksum, receipt, u.digest(result), "a" * 40,
                                    p.bindir, SERVICE, SERVICE, u.RELEASE, port=rehearsal.free_port())
        u.Cluster(p.root / "pg", p.bindir, SERVICE, SERVICE).stopped()      # Confirmed shut down.
    finally:
        pg.cluster.start(pg.socket, pg.port)
    assert (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_ino) == before
    assert not (p.root / "settings.env.upgrade-new").exists()


def test_tools_contain_no_assert_statements():
    """Every safety check is an explicit Refused, so python -O cannot remove one."""
    import ast
    for name in ("upgrade_support.py", "live_upgrade.py", "rehearse_upgrade.py"):
        tree = ast.parse((Path(u.__file__).parent / name).read_text())
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Assert)], name


# Recovery: restore a verified cold backup into the same root, then start the restored code's checks.

def old_code_startup(world_, port):
    """The restored release's own V1 startup gate (afa247a), against the restored database, as the runtime
    login. Requires KALILLAC_RESTORED_CODE: that release's candidate/ (CI extracts it with git archive)."""
    code = os.environ.get("KALILLAC_RESTORED_CODE")
    if not code or not Path(code, "kalillac_accounts/v1_security.py").is_file():
        pytest.fail("KALILLAC_RESTORED_CODE must name the restored release's candidate/ directory")
    secret = Path(tempfile.mkdtemp(prefix="ku-restore-")) / "runtime-url"
    secret.write_text(world_.runtime_engine.url.set(host="127.0.0.1", port=port).render_as_string(hide_password=False))
    os.chmod(secret, 0o600)
    script = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from kalillac_accounts.v1_security import FLAG, load_v1_startup
engine = create_engine(open(sys.argv[2]).read().strip(), hide_parameters=True)
factory = sessionmaker(engine)
environ = {FLAG: "true", "KALILLAC_ACCOUNTS_ENABLED": "true", "KALILLAC_DB_ENABLED": "true",
           "KALILLAC_DEPLOYMENT_ID": "kalillac-local-staging", "KALILLAC_RELEASE_SHA": "a" * 40}
qualified = load_v1_startup(lambda: factory, environ) is not None
print(json.dumps({"qualified": qualified}))
"""
    try:
        result = subprocess.run([sys.executable, "-B", "-c", script, code, str(secret)], capture_output=True,
                                text=True, cwd=code, timeout=120)
    finally:
        shutil.rmtree(secret.parent)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_restore_after_a_failed_upgrade_into_the_same_root_and_start_the_restored_code(prepared, pg, world, tmp_path):  # noqa: F811
    """The manual recovery, end to end on disposable installations: upgrade (and then damage) the root, decide
    to recover, restore the verified cold backup into the SAME root folder, check everything against the
    backup's manifest, as the service identity, and with the restored release's own startup gate."""
    p, w = prepared, world
    receipt = tmp_path / "receipt.json"
    result = rehearsal.rehearse(p.backup, p.checksum, p.bindir, receipt)
    identity = (p.root.stat().st_dev, p.root.stat().st_ino)
    port = rehearsal.free_port()
    pg.cluster.stop()                           # The conservative service-uid guard sees the source fixture.
    try:
        live_tool.apply_upgrade(p.root, p.backup, p.checksum, receipt, u.digest(result), "a" * 40,
                                p.bindir, SERVICE, SERVICE, u.RELEASE, port=port)
        cluster = u.Cluster(p.root / "pg", p.bindir, SERVICE, SERVICE)
        cluster.start(p.root / "pgsock", port)  # Post-upgrade writes that the restore will discard.
        admin, migration = u.engines(p.root, port)
        with admin.begin() as c:
            c.execute(text("UPDATE kalillac.messages SET content='written after the backup'"))
        admin.dispose()
        migration.dispose()
        (p.root / "state/files/written-after-backup").write_text("x")
        cluster.stop()

        failed = pg.base / ("failed-" + uuid.uuid4().hex[:6])
        with pytest.raises(u.Refused, match="another destination"):        # Never someone else's backup.
            other = pg.base / ("other-" + uuid.uuid4().hex[:6])
            other.mkdir()
            live_tool.restore_install(other, p.backup, p.checksum, failed, p.bindir, SERVICE, SERVICE)
        with pytest.raises(u.Refused, match="outside the installation"):
            live_tool.restore_install(p.root, p.backup, p.checksum, p.root / "failed", p.bindir, SERVICE, SERVICE)
        cluster.start(p.root / "pgsock", port)
        with pytest.raises(u.Refused, match="running|service identity"):  # Not while PostgreSQL runs.
            live_tool.restore_install(p.root, p.backup, p.checksum, failed, p.bindir, SERVICE, SERVICE)
        cluster.stop()
        assert not os.path.lexists(failed)

        restored = live_tool.restore_install(p.root, p.backup, p.checksum, failed, p.bindir, SERVICE, SERVICE)
        assert restored == {"restored": True, "failed_state": str(failed), "runtime": result["runtime"]}
        # The same root folder (the F1 marker names its inode), exactly the backup's contents and permissions.
        assert (p.root.stat().st_dev, p.root.stat().st_ino) == identity
        manifest = u.validate_backup(p.backup, p.checksum)
        assert u.tree_manifest(p.root) == manifest["files"]
        # The failed upgrade is evidence, preserved: its settings additions, its post-backup file, its manifest.
        assert "KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_FREE" in (failed / "settings.env").read_text()
        assert (failed / "state/files/written-after-backup").exists()
        assert json.loads(failed.with_name(failed.name + ".manifest.json").read_text())["files"]
        # The database, the files and the runtime files, as the service identity.
        cluster.start(p.root / "pgsock", port)
        admin, migration = u.engines(p.root, port)
        try:
            u.same_contents(p.before, u.database_snapshot(admin))            # 0025, every row as backed up.
            assert rehearsal.stored_files(p.root, admin, SERVICE, SERVICE) == result["files"]
            assert u.runtime_access(p.root, p.root, SERVICE, SERVICE) == result["runtime"]
            u.database_identity(admin, p.root / "pg", p.deployment["KALILLAC_DB_NAME"], "a" * 40)
        finally:
            admin.dispose()
            migration.dispose()
        # The restored release's own startup gate accepts the restored database.
        assert old_code_startup(w, port) == {"qualified": True}           # 0025 itself: same_contents above.
        cluster.stop()
    finally:
        pg.cluster.start(pg.socket, pg.port)


def test_new_refusals_survive_python_optimization(tmp_path):
    """Under python -O (asserts removed), a redirecting connection URL and a symlinked settings file are still
    refused: every check is an explicit Refused."""
    fake_payload(tmp_path, "postgresql+psycopg://postgres:s@127.0.0.1:5432/postgres?port=41109")
    (tmp_path / "real.env").write_text("A=1\n")
    (tmp_path / "settings.env").symlink_to(tmp_path / "real.env")
    script = (f"import sys;sys.path[:0]=[{str(Path(u.__file__).parent)!r}];"
              "from pathlib import Path;import upgrade_support as u, live_upgrade as l;"
              f"p=Path({str(tmp_path)!r});out=[]\n"
              "for call in (lambda: u.engines(p / 'payload', 55999),"
              " lambda: l.stage_settings(p / 'settings.env', l.SETTINGS_ADDITION)):\n"
              "    try:\n        call(); out.append('ACCEPTED')\n"
              "    except u.Refused:\n        out.append('refused')\n"
              "print(','.join(out))")
    result = subprocess.run([sys.executable, "-O", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "refused,refused", result.stderr[-2000:]
