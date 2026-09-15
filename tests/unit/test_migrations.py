"""Schema migrations.

Driven the way they actually run: write an older shape to disk, run the engine
against the directory, and check what came out the other side.
"""
import json
import os
import shutil
import subprocess
import sys
import unicodedata
from unittest import mock

import pytest

import steerholm.config as _cfg
from steerholm import migrations
from steerholm.migrations import MigrationError, MigrationStore
from steerholm.migrations.store import StateError
from steerholm.config import ConfigManager, SchemaError


def _v0_state(root, *, agents=("bob",), servers=("git",), grants=True):
    """A state directory shaped the way 0.1.x wrote one: no version marker,
    grants keyed by name, ids absent."""
    (root / "policies").mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({
        "servers": {s: {"name": s, "command": "echo", "url": "",
                        "env": {}, "server_type": "stdio"} for s in servers},
        "agents": {a: {"name": a, "key_prefix": "steer_sk_x..."} for a in agents},
    }, indent=2))
    if grants:
        for a in agents:
            (root / "policies" / f"{a}.json").write_text(json.dumps({
                "agent_name": a,
                "permissions": {s: [{"name": "read_*", "policies": []}] for s in servers},
            }, indent=2))
    return root


@pytest.fixture
def state(tmp_path):
    return _v0_state(tmp_path / "state")


# ─── the version marker ─────────────────────────────────────────────


def test_state_without_a_marker_reads_as_pre_versioning(state):
    # The store reports the absence; the migrations decide what it means. Both
    # releases that could have written a marker wrote the same shape, so the
    # baseline is a constant rather than an inference from the document.
    store = MigrationStore(state)
    assert store.version is None
    assert migrations.resolve_version(store) == migrations.UNVERSIONED_BASELINE == 0


def test_the_marker_lives_beside_the_state_not_inside_the_config(state):
    migrations.run(state)
    assert (state / "migrations" / "version").read_text().strip() == "1"
    # It describes the whole directory, so it is not a config field.
    assert "version" not in json.loads((state / "config.json").read_text())


def test_a_marker_that_is_present_but_unreadable_is_not_treated_as_absent(state):
    # Collapsing "cannot read it" into "predates versioning" would re-run
    # migrations against already-migrated data and destroy the backup.
    (state / "migrations").mkdir()
    for garbage in ("not a number", "", "-3", "1_0"):
        (state / "migrations" / "version").write_text(garbage)
        with pytest.raises(StateError):
            MigrationStore(state).version
        with pytest.raises(MigrationError):
            migrations.run(state)


def test_an_absent_marker_reads_as_pre_versioning(state):
    assert MigrationStore(state).version is None


# ─── what m0001 does ────────────────────────────────────────────────


def test_ids_are_backfilled(state):
    migrations.run(state)
    config = json.loads((state / "config.json").read_text())
    assert config["agents"]["bob"]["id"].startswith("agt_")
    assert config["servers"]["git"]["id"].startswith("srv_")


def test_backfilled_ids_are_derived_so_concurrent_runs_agree(tmp_path):
    a = _v0_state(tmp_path / "a")
    b = _v0_state(tmp_path / "b")
    migrations.run(a)
    migrations.run(b)
    assert (json.loads((a / "config.json").read_text())["agents"]["bob"]["id"]
            == json.loads((b / "config.json").read_text())["agents"]["bob"]["id"])


def test_an_existing_id_is_left_alone(state):
    config = json.loads((state / "config.json").read_text())
    config["agents"]["bob"]["id"] = "agt_deadbeefdeadbeef"
    (state / "config.json").write_text(json.dumps(config))
    migrations.run(state)
    assert json.loads((state / "config.json").read_text())["agents"]["bob"]["id"] \
        == "agt_deadbeefdeadbeef"


def test_the_policy_is_renamed_to_the_agent_id(state):
    migrations.run(state)
    agent_id = json.loads((state / "config.json").read_text())["agents"]["bob"]["id"]
    assert (state / "policies" / f"{agent_id}.json").exists()
    assert not (state / "policies" / "bob.json").exists()


def test_grants_are_rekeyed_to_the_server_id(state):
    migrations.run(state)
    config = json.loads((state / "config.json").read_text())
    server_id = config["servers"]["git"]["id"]
    doc = json.loads((state / "policies" / f"{config['agents']['bob']['id']}.json").read_text())
    assert list(doc["permissions"]) == [server_id]
    assert doc["permissions"][server_id][0]["name"] == "read_*"
    assert doc["agent_id"] == config["agents"]["bob"]["id"]


def test_argument_policies_survive(tmp_path):
    state = _v0_state(tmp_path / "s", grants=False)
    (state / "policies" / "bob.json").write_text(json.dumps({"permissions": {"git": [
        {"name": "read_file",
         "policies": [{"arg_name": "path", "match_type": "glob", "pattern": "/tmp/**"}]}]}}))
    migrations.run(state)
    config = json.loads((state / "config.json").read_text())
    doc = json.loads((state / "policies" / f"{config['agents']['bob']['id']}.json").read_text())
    tool = doc["permissions"][config["servers"]["git"]["id"]][0]
    assert tool["policies"][0]["pattern"] == "/tmp/**"


def test_a_grant_naming_a_server_that_is_gone_is_dropped(tmp_path, caplog):
    # There is no id to point it at, and it could never have matched anything.
    state = _v0_state(tmp_path / "s", grants=False)
    (state / "policies" / "bob.json").write_text(json.dumps({"permissions": {
        "git": [{"name": "read_*", "policies": []}],
        "ghost": [{"name": "*", "policies": []}]}}))
    migrations.run(state)
    config = json.loads((state / "config.json").read_text())
    doc = json.loads((state / "policies" / f"{config['agents']['bob']['id']}.json").read_text())
    assert list(doc["permissions"]) == [config["servers"]["git"]["id"]]
    assert "ghost" in caplog.text


def test_a_policy_with_no_agent_in_the_config_is_set_aside(tmp_path, caplog):
    # Nothing to key it under, and leaving it in `policies/` would end the step
    # with a schema-0 document in a directory the marker calls schema 1.
    state = _v0_state(tmp_path / "s")
    (state / "policies" / "orphan.json").write_text(json.dumps({"permissions": {}}))

    migrations.run(state)

    assert not (state / "policies" / "orphan.json").exists()
    assert (state / "policies" / "unmigrated" / "orphan.json").exists()
    assert "orphan" in caplog.text


def test_a_legacy_name_that_is_not_filename_safe_keeps_its_grants(tmp_path):
    # 0.1.x had no name validation, so these are real agent names on real
    # installs. Refusing them would strip their grants while reporting success.
    state = _v0_state(tmp_path / "s", agents=("2024-bot", "my agent", "café"))

    migrations.run(state)

    config = json.loads((state / "config.json").read_text())
    for name in ("2024-bot", "my agent", "café"):
        doc = state / "policies" / f"{config['agents'][name]['id']}.json"
        assert doc.exists(), name
        assert json.loads(doc.read_text())["permissions"]



def test_policies_are_converted_even_when_the_config_has_no_agents(tmp_path):
    # Otherwise the marker says schema 1 while `policies/` still holds
    # name-keyed documents.
    state = _v0_state(tmp_path / "s", grants=False)
    (state / "config.json").write_text(json.dumps({"servers": {}, "agents": {}}))
    (state / "policies" / "ghost.json").write_text(json.dumps({"permissions": {}}))

    migrations.run(state)

    assert not (state / "policies" / "ghost.json").exists()
    assert (state / "policies" / "unmigrated" / "ghost.json").exists()




def test_running_twice_applies_nothing_the_second_time(state):
    assert migrations.run(state) == 1
    assert migrations.run(state) == 0


def test_the_marker_and_the_data_move_together(state):
    migrations.run(state)
    store = MigrationStore(state)
    agent_id = json.loads((state / "config.json").read_text())["agents"]["bob"]["id"]
    # the marker never claims a shape the files are not in
    assert store.version == 1
    assert (state / "policies" / f"{agent_id}.json").exists()


def test_a_step_that_fails_leaves_the_live_state_untouched(state):
    before = {p.name: p.read_bytes() for p in state.rglob("*") if p.is_file()}

    with mock.patch.dict(migrations.MIGRATIONS,
                         {1: lambda s: (_ for _ in ()).throw(RuntimeError("boom"))}):
        with pytest.raises(MigrationError, match="nothing was changed"):
            migrations.run(state)

    after = {p.name: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    assert after == before
    assert migrations.resolve_version(MigrationStore(state)) == 0          # not bumped
    assert not (state / ".migrating-v1").exists()


def test_the_pre_migration_state_is_kept(state):
    migrations.run(state)
    backup = migrations.backup_path(state)
    assert (backup / "policies" / "bob.json").exists()   # the name-keyed original


def test_a_retry_does_not_overwrite_an_existing_backup(state):
    # After a crash mid-swap the existing backup may hold the ONLY copy of the
    # pre-migration shape; a retry must not refill it with migrated files.
    migrations.run(state)
    original = (migrations.backup_path(state) / "policies" / "bob.json").read_bytes()

    _v0_state(state)                       # a second unmigrated state, same dir
    MigrationStore(state).version_file.unlink()
    migrations.run(state)

    kept = migrations.backup_path(state) / "policies" / "bob.json"
    assert kept.read_bytes() == original   # untouched


def test_the_audit_log_is_not_touched(state):
    (state / "events-000001.jsonl").write_text('{"ts":"2026-01-01T00:00:00"}\n')
    migrations.run(state)
    # Not staged, not swapped, not moved aside: it is large, rotated, and no
    # step reads it.
    assert (state / "events-000001.jsonl").exists()
    assert not (migrations.backup_path(state) / "events-000001.jsonl").exists()


def test_state_newer_than_this_build_is_refused(state):
    MigrationStore(state).version = migrations.CURRENT_VERSION + 1
    with pytest.raises(MigrationError, match="no reverse migrations"):
        migrations.run(state)


def test_an_empty_directory_migrates_to_nothing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert migrations.run(empty) == 1        # the step runs and finds nothing
    assert MigrationStore(empty).version == 1


# ─── how the rest of the code reacts ────────────────────────────────


def test_a_command_refuses_against_unmigrated_state(config_manager):
    _v0_state(_cfg.CONFIG_DIR)
    # The fixture's manager stamped the fresh dir; an upgrading install has a
    # config but no marker, which is what schema 0 looks like.
    MigrationStore(_cfg.CONFIG_DIR).version_file.unlink()
    with pytest.raises(SchemaError, match="expects v1"):
        ConfigManager.verify_schema()


def test_a_command_refuses_against_state_from_a_newer_build(config_manager):
    _v0_state(_cfg.CONFIG_DIR)
    MigrationStore(_cfg.CONFIG_DIR).version = migrations.CURRENT_VERSION + 1
    with pytest.raises(SchemaError, match="Upgrade Steerholm"):
        ConfigManager.verify_schema()


def test_verification_passes_once_migrated(config_manager):
    _v0_state(_cfg.CONFIG_DIR)
    MigrationStore(_cfg.CONFIG_DIR).version_file.unlink()
    migrations.run(_cfg.CONFIG_DIR)
    ConfigManager.verify_schema()            # must not raise


def test_a_fresh_install_is_stamped_current(config_manager):
    # Otherwise the first `holm add server` writes a config with no marker and
    # every later command reads it as schema 0 and refuses.
    assert MigrationStore(_cfg.CONFIG_DIR).version == migrations.CURRENT_VERSION
    config_manager.add_server("git", command="echo")
    ConfigManager.verify_schema()


def test_construction_never_refuses(config_manager):
    # The CLI builds a manager at import; refusing there would take down the
    # `migrate` command along with everything else.
    _v0_state(_cfg.CONFIG_DIR)
    MigrationStore(_cfg.CONFIG_DIR).version_file.unlink()
    ConfigManager()                          # must not raise


# ─── error paths ────────────────────────────────────────────────────


def test_a_policy_whose_agent_name_starts_with_a_digit_still_migrates(tmp_path):
    state = _v0_state(tmp_path / "s", agents=("9bad",))

    migrations.run(state)

    config = json.loads((state / "config.json").read_text())
    assert (state / "policies" / f"{config['agents']['9bad']['id']}.json").exists()





def test_a_swap_that_fails_at_the_last_name_still_rolls_back(state):
    # `migrations` moves last, so by then config.json and policies/ are already
    # swapped in — the rollback has to undo a directory, which os.replace alone
    # cannot do over a non-empty one.
    real_replace = os.replace

    def fail_on_marker(src, dst, *a, **k):
        if (".migrating-v" in str(src) and ".migrating-v" not in str(dst)
                and str(dst).endswith(os.sep + "version")):
            raise OSError("read-only")
        return real_replace(src, dst, *a, **k)

    with mock.patch("os.replace", fail_on_marker):
        with pytest.raises(MigrationError, match="could not be swapped in"):
            migrations.run(state)

    assert (state / "policies" / "bob.json").exists()      # name-keyed original
    assert "id" not in json.loads((state / "config.json").read_text())["agents"]["bob"]
    assert migrations.resolve_version(MigrationStore(state)) == 0
    assert migrations.run(state) == 1                      # and still migratable


def test_a_swap_that_fails_puts_back_what_it_moved(state):
    before = (state / "config.json").read_bytes()
    real_replace = os.replace

    def fail_staged_to_live(src, dst, *a, **k):
        # Only the staged -> live direction; the rollback must be free to run.
        if "migrating-v" in str(src) and os.path.basename(str(dst)) == "policies":
            raise OSError("read-only")
        return real_replace(src, dst, *a, **k)

    with mock.patch("os.replace", fail_staged_to_live):
        with pytest.raises(MigrationError, match="could not be swapped in"):
            migrations.run(state)

    # The live state must still be USABLE, not merely unchanged in one file:
    # a rollback that forgets a name it already moved aside leaves the state
    # directory without its policies, and every agent without its grants.
    assert (state / "config.json").read_bytes() == before
    assert (state / "policies" / "bob.json").exists()
    assert migrations.resolve_version(MigrationStore(state)) == 0
    # and re-running still works from that state
    assert migrations.run(state) == 1


def test_a_state_with_no_policies_directory_migrates(tmp_path):
    state = _v0_state(tmp_path / "s", grants=False)
    (state / "policies").rmdir()
    assert MigrationStore(state).policy_files() == []
    migrations.run(state)
    assert MigrationStore(state).version == 1



def test_a_marker_for_a_step_this_build_lacks_is_reported(state):
    # The state is at 0 and CURRENT_VERSION is 1, so step 1 is pending; with no
    # migration registered for it the runner must say so rather than KeyError.
    with mock.patch.dict(migrations.MIGRATIONS, {}, clear=True):
        with pytest.raises(MigrationError, match="missing the migration"):
            migrations.run(state)


def test_a_gap_in_the_table_is_caught_before_anything_is_applied(state):
    # Applying part of the range and then stopping would leave the state at an
    # intermediate version while the error still named the version it started at.
    with mock.patch.object(migrations, "CURRENT_VERSION", 3), \
         mock.patch.dict(migrations.MIGRATIONS, {1: migrations.MIGRATIONS[1], 3: lambda s: None},
                         clear=True):
        with pytest.raises(MigrationError, match="v2"):
            migrations.run(state)

    assert migrations.resolve_version(MigrationStore(state)) == 0            # step 1 never ran
    assert (state / "policies" / "bob.json").exists()


def test_a_marker_that_cannot_be_read_is_reported_not_assumed(state):
    store = MigrationStore(state)
    store.version = 1
    store.version_file.chmod(0o000)
    try:
        with pytest.raises(StateError, match="cannot be read"):
            store.version
    finally:
        store.version_file.chmod(0o600)


def test_an_unreadable_marker_makes_commands_refuse_not_guess(config_manager):
    _v0_state(_cfg.CONFIG_DIR)
    store = MigrationStore(_cfg.CONFIG_DIR)
    store.version = 1
    store.version_file.chmod(0o000)
    try:
        with pytest.raises(SchemaError, match="cannot be read"):
            ConfigManager.verify_schema()
    finally:
        store.version_file.chmod(0o600)


def test_a_rollback_that_cannot_restore_says_which_pieces_are_stranded(state):
    real_replace = os.replace

    def fail_swap_and_rollback(src, dst, *a, **k):
        if (".migrating-v" in str(src) and ".migrating-v" not in str(dst)
                and str(dst).endswith(os.sep + "version")):
            raise OSError("read-only")
        if ".pre-migration" in str(src):        # the rollback itself
            raise OSError("also read-only")
        return real_replace(src, dst, *a, **k)

    with mock.patch("os.replace", fail_swap_and_rollback):
        with pytest.raises(MigrationError, match="only at"):
            migrations.run(state)


def test_a_genuinely_empty_state_dir_does_not_refuse(tmp_path, monkeypatch):
    # Nothing on disk is not the same as something old on disk.
    import steerholm.config as c
    monkeypatch.setattr(c, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(c, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(c, "POLICIES_DIR", tmp_path / "policies")
    ConfigManager.verify_schema()          # must not raise


def test_permissions_are_not_fsynced_on_windows(state):
    with mock.patch.object(migrations.os, "name", "nt"), \
         mock.patch.object(migrations.os, "open") as opened:
        migrations._fsync_dir(state)
    opened.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows uses ACLs")
def test_staging_and_backup_are_owner_only(state):
    # They are SIBLINGS of the state directory, so the 0700 that protects the
    # live one does not cover them — and they hold a full copy of it, `--env`
    # secrets and key prefixes included.
    migrations.run(state)

    backup = migrations.backup_path(state)
    assert backup.stat().st_mode & 0o777 == 0o700
    marker = state / "migrations" / "version"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert (state / "migrations").stat().st_mode & 0o777 == 0o700
    for policy in (state / "policies").glob("*.json"):
        assert policy.stat().st_mode & 0o777 == 0o600


def test_a_state_missing_a_name_the_backup_holds_is_refused(state):
    # A rollback that could not restore a directory leaves it only in the
    # backup. Migrating from there would convert a state with no policies and
    # stamp it current — turning "in the backup" into "gone".
    backup = migrations.backup_path(state)
    (backup / "policies").mkdir(parents=True)
    (backup / "policies" / "bob.json").write_text("{}")
    shutil.rmtree(state / "policies")

    with pytest.raises(MigrationError, match="missing from"):
        migrations.run(state)


def test_only_one_migration_runs_at_a_time(state):
    lock = state / ".migrating.lock"
    lock.write_text(f"{os.getppid()}\n")      # a process that is actually alive
    with pytest.raises(MigrationError, match="Another migration is running"):
        migrations.run(state)
    lock.unlink()
    assert migrations.run(state) == 1


@pytest.mark.skipif(os.name == "nt", reason="os.kill cannot ask this on Windows")
def test_a_lock_left_by_a_dead_process_does_not_block_forever(state):
    # A SIGKILL or a power cut mid-migration leaves the file behind. Refusing on
    # it would fail every later run — and both installers abort the whole
    # install when `holm migrate` fails.
    done = subprocess.Popen([sys.executable, "-c", ""])
    done.wait()
    (state / ".migrating.lock").write_text(f"{done.pid}\n")

    assert migrations.run(state) == 1
    assert not (state / ".migrating.lock").exists()


@pytest.mark.skipif(os.name == "nt", reason="os.kill cannot ask this on Windows")
def test_an_unreadable_lock_is_left_alone(state):
    # Staleness is a claim that needs evidence. Without a PID to check, the
    # safe answer is that someone owns it: clearing a live lock would let two
    # migrations share one staging directory.
    (state / ".migrating.lock").write_text("not a pid\n")
    with pytest.raises(MigrationError, match="Another migration is running"):
        migrations.run(state)


def test_the_lock_is_released_when_a_step_fails(state):
    with mock.patch.dict(migrations.MIGRATIONS,
                         {1: lambda s: (_ for _ in ()).throw(RuntimeError("boom"))}):
        with pytest.raises(MigrationError):
            migrations.run(state)
    assert not (state / ".migrating.lock").exists()
    assert migrations.run(state) == 1          # and the retry is not blocked


def test_a_swap_that_displaced_nothing_keeps_staging_for_the_retry(state):
    # Failing on the very FIRST displacement rolls nothing back, so live still
    # matches what staging expects and the retry can finish the swap rather than
    # redo the step against data it may already have converted.
    real_replace = os.replace

    def fail_first_displacement(src, dst, *a, **k):
        if ".pre-migration" in str(dst):
            raise OSError("read-only")
        return real_replace(src, dst, *a, **k)

    with mock.patch("os.replace", side_effect=fail_first_displacement):
        with pytest.raises(MigrationError):
            migrations.run(state)

    staging = state / ".migrating-v1"
    assert staging.exists()
    if os.name != "nt":
        assert oct(staging.stat().st_mode)[-3:] == "700"   # a copy of the state

    assert migrations.run(state) == 1
    assert not staging.exists()
    config = json.loads((state / "config.json").read_text())
    assert (state / "policies" / f"{config['agents']['bob']['id']}.json").exists()


def test_a_swap_that_fails_during_a_resume_does_not_cost_the_originals(state):
    # Two failures in a row: a crash mid-swap, then a transient error on the
    # retry. Without staging surviving the second one, the third attempt
    # rebuilds from converted live data and overwrites the backup with it.
    original = (state / "config.json").read_bytes()
    real = os.replace

    def crash_before_policies(src, dst, *a, **k):
        if ".migrating-v" in str(src) and str(dst).endswith(os.sep + "policies"):
            raise KeyboardInterrupt("power cut")
        return real(src, dst, *a, **k)

    def fail_on_marker(src, dst, *a, **k):
        if (".migrating-v" in str(src) and ".migrating-v" not in str(dst)
                and str(dst).endswith(os.sep + "version")):
            raise OSError("transient EIO")
        return real(src, dst, *a, **k)

    with mock.patch("os.replace", side_effect=crash_before_policies):
        with pytest.raises(KeyboardInterrupt):
            migrations.run(state)
    with mock.patch("os.replace", side_effect=fail_on_marker):
        with pytest.raises(MigrationError):
            migrations.run(state)

    assert migrations.run(state) == 1

    kept = migrations.backup_path(state)
    assert (kept / "config.json").read_bytes() == original
    assert (kept / "policies" / "bob.json").exists()
    assert migrations.resolve_version(MigrationStore(state)) == 1


def test_a_marker_that_is_not_decodable_text_is_reported(state):
    store = MigrationStore(state)
    store.version = 1
    store.version_file.write_bytes(b"\xff\xfe1")
    with pytest.raises(StateError, match="not readable text"):
        store.version


def test_the_lock_survives_a_missing_state_directory(tmp_path):
    # run() is given a directory that does not exist yet.
    fresh = tmp_path / "never-created"
    assert migrations.run(fresh) == 1
    assert not (fresh / ".migrating.lock").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_restrict_is_a_no_op_on_windows(tmp_path):
    from steerholm.migrations import store as store_mod
    target = tmp_path / "f"
    target.write_text("x")
    target.chmod(0o644)
    with mock.patch.object(store_mod.os, "name", "nt"):
        store_mod.restrict(target, 0o600)
    assert target.stat().st_mode & 0o777 == 0o644     # untouched



def test_an_already_migrated_policy_is_not_set_aside(tmp_path):
    # The crash-recovery case the id-shape check exists for: the policy write
    # landed, the marker did not. Re-running must leave it alone — treating it as
    # a policy naming no agent would file a live grant under `unmigrated/`.
    agent_id, server_id = "agt_" + "a" * 16, "srv_" + "b" * 16
    state = _v0_state(tmp_path / "s", grants=False)
    config = json.loads((state / "config.json").read_text())
    config["agents"]["bob"]["id"] = agent_id
    config["servers"]["git"]["id"] = server_id
    (state / "config.json").write_text(json.dumps(config))
    (state / "policies" / f"{agent_id}.json").write_text(json.dumps(
        {"agent_id": agent_id,
         "permissions": {server_id: [{"name": "read_*", "policies": []}]}}))

    migrations.run(state)

    doc = json.loads((state / "policies" / f"{agent_id}.json").read_text())
    assert list(doc["permissions"]) == [server_id]
    assert not (state / "policies" / "unmigrated").exists()


def test_an_agent_named_empty_string_keeps_its_grants(tmp_path):
    # Reachable on 0.1.x, which validated no names: whether `add agent ""`
    # succeeded came down to the keyring backend, and the common ones allow a
    # blank username. It writes the policy to a bare `.json`, whose `.stem`
    # pathlib reports as ".json" rather than "".
    state = _v0_state(tmp_path / "s", agents=("",))

    migrations.run(state)

    config = json.loads((state / "config.json").read_text())
    doc = state / "policies" / f"{config['agents']['']['id']}.json"
    assert doc.exists() and json.loads(doc.read_text())["permissions"]
    assert not (state / "policies" / ".json").exists()


def test_the_unmigrated_directory_is_not_mistaken_for_a_policy(tmp_path):
    # It lives inside `policies/`, so a second run must not try to read it.
    state = _v0_state(tmp_path / "s", grants=False)
    (state / "policies" / "ghost.json").write_text(json.dumps({"permissions": {}}))
    migrations.run(state)

    store = MigrationStore(state)
    assert (state / "policies" / "unmigrated").is_dir()
    assert store.policy_files() == []



def test_a_decomposed_filename_resolves_to_the_composed_config_key(tmp_path):
    # HFS+ stores filenames decomposed, so a name typed as NFC reads back NFD
    # while config.json, being JSON, still holds the NFC. Same text, so the same
    # agent — matching on bytes would file its grants under `unmigrated/`.
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert nfc != nfd
    state = _v0_state(tmp_path / "s", agents=(nfc,), grants=False)
    (state / "policies" / f"{nfd}.json").write_text(json.dumps(
        {"agent_name": nfc, "permissions": {"git": [{"name": "read_*", "policies": []}]}}))

    migrations.run(state)

    config = json.loads((state / "config.json").read_text())
    doc = state / "policies" / f"{config['agents'][nfc]['id']}.json"
    assert doc.exists(), "the grant was filed as an orphan"
    assert json.loads(doc.read_text())["permissions"]


def test_a_composed_filename_resolves_to_a_decomposed_config_key(tmp_path):
    # The same drift in the other direction.
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    state = _v0_state(tmp_path / "s", agents=(nfd,), grants=False)
    (state / "policies" / f"{nfc}.json").write_text(json.dumps(
        {"permissions": {"git": [{"name": "read_*", "policies": []}]}}))

    migrations.run(state)

    config = json.loads((state / "config.json").read_text())
    assert (state / "policies" / f"{config['agents'][nfd]['id']}.json").exists()


def test_a_policy_claiming_a_different_agent_is_not_handed_over(tmp_path):
    # On a case-insensitive filesystem 0.1.x wrote agents `CI` and `ci` to one
    # and the same file: whichever wrote last owns the contents, while the
    # directory listing still yields the other's name. Re-keying on the filename
    # would move one agent's grants to a different principal — the transfer this
    # whole step exists to make impossible.
    state = _v0_state(tmp_path / "s", agents=("CI", "ci"), grants=False)
    (state / "policies" / "CI.json").write_text(json.dumps(
        {"agent_name": "ci", "permissions": {"git": [{"name": "read_*", "policies": []}]}}))

    migrations.run(state)

    config = json.loads((state / "config.json").read_text())
    for name in ("CI", "ci"):
        assert not (state / "policies" / f"{config['agents'][name]['id']}.json").exists()
    assert (state / "policies" / "unmigrated" / "CI.json").exists()


def test_a_policy_claiming_an_agent_that_is_gone_still_migrates(tmp_path):
    # Only a claim naming a *different real* agent is ambiguous. A stale one
    # names nobody, so the filename is the better evidence.
    state = _v0_state(tmp_path / "s", agents=("bob",), grants=False)
    (state / "policies" / "bob.json").write_text(json.dumps(
        {"agent_name": "ghost", "permissions": {"git": [{"name": "read_*", "policies": []}]}}))

    migrations.run(state)

    config = json.loads((state / "config.json").read_text())
    assert (state / "policies" / f"{config['agents']['bob']['id']}.json").exists()


def test_a_reset_state_beside_an_old_backup_still_migrates(state):
    # A backup outlives the migration that made it. Refusing whenever one holds
    # a name the live directory lacks would fail every later run against a state
    # the user reset on purpose — and both installers abort when migrate fails.
    migrations.run(state)
    shutil.rmtree(state)

    assert migrations.run(state) == 1        # nothing pending, nothing to strand


def test_a_backup_that_cannot_be_created_is_reported_and_leaks_nothing(state):
    # It holds a copy of config.json, --env secrets and all.
    real = os.mkdir

    def fail_on_backup(name, *a, **k):
        if ".pre-migration" in os.path.basename(name):
            raise PermissionError(13, "Permission denied")
        return real(name, *a, **k)

    with mock.patch("os.mkdir", side_effect=fail_on_backup):
        with pytest.raises(MigrationError, match="could not be swapped in"):
            migrations.run(state)

    assert not list(state.parent.glob(f"{state.name}.migrating-v*"))
    assert (state / "policies" / "bob.json").exists()      # live state untouched


def test_a_pre_0_1_0_identities_key_is_refused_not_emptied(tmp_path):
    # Support for these states was dropped deliberately. Migrating anyway finds
    # no agents, files every policy under `unmigrated/` and reports success —
    # and the config that comes out does not load, because `Config` forbids
    # unknown keys, so every server and agent vanishes behind a warning.
    state = _v0_state(tmp_path / "s", grants=False)
    (state / "config.json").write_text(json.dumps({
        "servers": {"git": {"name": "git", "command": "echo"}},
        "identities": {"bob": {"name": "bob", "key_prefix": "steer_sk_x"}},
    }))
    (state / "policies" / "bob.json").write_text(json.dumps(
        {"agent_name": "bob", "permissions": {"git": []}}))

    with pytest.raises(MigrationError, match="identities"):
        migrations.run(state)

    assert (state / "policies" / "bob.json").exists()
    assert not (state / "migrations").exists()            # not stamped current


def test_an_unparseable_config_says_what_to_do_about_it(state):
    # ConfigManager has always tolerated this (warning + empty config), so the
    # upgrade must not turn it into a state with no in-product remedy — and the
    # message must name the live file, not the staging copy it is read from.
    (state / "config.json").write_text('{"servers": {"git": {"name": "gi')

    with pytest.raises(MigrationError) as e:
        migrations.run(state)

    assert str(state / "config.json") in str(e.value)
    assert "not valid JSON" in str(e.value)
    assert ".migrating-v" not in str(e.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_state_directory_the_engine_creates_is_owner_only(tmp_path):
    # It is about to receive config.json and every policy.
    root = tmp_path / "fresh"
    migrations.run(root)
    assert oct(root.stat().st_mode)[-3:] == "700"



class TestLockStaleness:
    """When a lock may be cleared. Every uncertain answer must keep it."""

    def _locked(self, state, body):
        (state / ".migrating.lock").write_text(body)
        return pytest.raises(MigrationError, match="Another migration is running")

    def test_windows_never_clears_one(self, state):
        # os.kill cannot ask the question there, and guessing "gone" would let
        # two migrations share one staging directory.
        done = subprocess.Popen([sys.executable, "-c", ""])
        done.wait()
        with mock.patch.object(os, "name", "nt"):
            with self._locked(state, f"{done.pid}\n"):
                migrations.run(state)

    def test_our_own_pid_is_not_stale(self, state):
        with self._locked(state, f"{os.getpid()}\n"):
            migrations.run(state)

    def test_a_nonsense_pid_is_not_stale(self, state):
        with self._locked(state, "0\n"):
            migrations.run(state)

    def test_a_pid_we_may_not_signal_is_not_stale(self, state):
        # EPERM means the process exists and belongs to someone else.
        with mock.patch.object(os, "kill", side_effect=PermissionError(1, "nope")):
            with self._locked(state, "12345\n"):
                migrations.run(state)


def test_a_crash_at_the_marker_rename_recovers_without_losing_grants(state):
    # The "marker last" ordering deliberately opens this window: policies/ is
    # renamed in id-keyed, the marker does not follow, so the retry starts from
    # schema 0 and finds a directory of id-named files. Reading those as
    # "policies naming no agent in the config" would file every live grant under
    # unmigrated/. This is the engine's own doing, not a hand-edited state.
    real = os.replace

    def die_at_the_marker(src, dst, *a, **k):
        if (".migrating-v" in str(src) and ".migrating-v" not in str(dst)
                and str(dst).endswith(os.sep + "version")):
            raise KeyboardInterrupt("power cut")
        return real(src, dst, *a, **k)

    with mock.patch("os.replace", side_effect=die_at_the_marker):
        with pytest.raises(KeyboardInterrupt):
            migrations.run(state)

    config = json.loads((state / "config.json").read_text())
    agent_id = config["agents"]["bob"]["id"]
    assert (state / "policies" / f"{agent_id}.json").exists()   # swapped in
    assert not (state / "migrations" / "version").exists()      # marker did not

    assert migrations.run(state) == 1

    doc = json.loads((state / "policies" / f"{agent_id}.json").read_text())
    assert doc["permissions"], "the grant was dropped on retry"
    assert not (state / "policies" / "unmigrated").exists()
    assert migrations.resolve_version(MigrationStore(state)) == 1


class TestTheKeptBackup:
    """One backup, one name. Which schema it holds is recorded inside it."""

    def test_it_lives_inside_the_state_directory(self, state):
        # It holds a copy of config.json, --env secrets and all. As a sibling it
        # sat outside the 0700 protecting the state directory.
        migrations.run(state)
        kept = migrations.backup_path(state)
        assert kept.parent == state and kept.name == ".pre-migration"
        assert (kept / "policies" / "bob.json").exists()
        assert not list(state.parent.glob(f"{state.name}.*"))

    def test_the_schema_it_holds_is_readable_from_inside_it(self, state):
        # This is what lets the name drop the version: an absent marker means
        # pre-versioning, the same rule the live state follows.
        migrations.run(state)
        kept = MigrationStore(migrations.backup_path(state))
        assert kept.version is None
        assert migrations.resolve_version(kept) == 0

    def test_a_second_migration_does_not_add_a_second_directory(self, state):
        migrations.run(state)
        _v0_state(state)
        MigrationStore(state).version_file.unlink()
        migrations.run(state)

        assert [p.name for p in state.glob(".pre-migration*")] == [".pre-migration"]
        assert not list(state.parent.glob(f"{state.name}.*"))

    def test_a_retry_after_a_crash_does_not_overwrite_the_originals(self, state):
        # The crashed run left the converted data live and the originals in the
        # backup. The retry's displaced files are that converted data — putting
        # them in the backup would lose the only name-keyed copy. The backup
        # reading the SAME version as the live state is what says so.
        real = os.replace

        def die_at_the_marker(src, dst, *a, **k):
            if (".migrating-v" in str(src) and ".migrating-v" not in str(dst)
                and str(dst).endswith(os.sep + "version")):
                raise KeyboardInterrupt("power cut")
            return real(src, dst, *a, **k)

        with mock.patch("os.replace", side_effect=die_at_the_marker):
            with pytest.raises(KeyboardInterrupt):
                migrations.run(state)
        kept = migrations.backup_path(state)
        original = (kept / "policies" / "bob.json").read_bytes()

        migrations.run(state)

        assert (kept / "policies" / "bob.json").read_bytes() == original

    def test_a_finished_migration_is_superseded_by_the_next_one(self, monkeypatch, state):
        # Once a run has finished, live has moved past what the backup holds, so
        # it is the state before a migration that is already done. The NEXT
        # migration should leave the user holding what they were actually
        # running, not a snapshot from two schemas back.
        migrations.run(state)
        assert migrations.resolve_version(MigrationStore(migrations.backup_path(state))) == 0
        (state / "policies" / "added-while-on-v1.json").write_text(
            json.dumps({"agent_id": "agt_" + "c" * 16, "permissions": {}}))

        monkeypatch.setitem(migrations.MIGRATIONS, 2, lambda store: None)
        monkeypatch.setattr(migrations, "CURRENT_VERSION", 2)
        assert migrations.run(state) == 1

        kept = migrations.backup_path(state)
        assert migrations.resolve_version(MigrationStore(kept)) == 1
        assert (kept / "policies" / "added-while-on-v1.json").exists(), \
            "kept the v0 snapshot instead of the state the user was running"

    def test_nothing_is_left_behind_beside_the_state(self, state):
        migrations.run(state)
        assert not list(state.parent.glob(f"{state.name}.*"))   # no siblings at all
        assert sorted(p.name for p in state.iterdir()) == [
            ".pre-migration", "config.json", "migrations", "policies"]


class TestResumingAnInterruptedSwap:
    """The staging copy is the progress record; the renames maintain it."""

    def _crash_at(self, target):
        """Crash on the rename that brings `target` out of staging into live.

        `.migrating-v` must be absent from the destination: the staged marker is
        written with a temp-file rename inside staging whose destination also
        ends in "version", and matching that would crash before any swap.
        """
        real = os.replace

        def die(src, dst, *a, **k):
            if (".migrating-v" in str(src) and ".migrating-v" not in str(dst)
                    and str(dst).endswith(os.sep + target)):
                raise KeyboardInterrupt("power cut")
            return real(src, dst, *a, **k)
        return mock.patch("os.replace", side_effect=die)

    def _counted(self, monkeypatch):
        runs = []
        real = migrations.MIGRATIONS[1]

        def counting(store):
            runs.append(1)
            return real(store)
        monkeypatch.setitem(migrations.MIGRATIONS, 1, counting)
        return runs

    def test_the_step_is_not_run_a_second_time(self, monkeypatch, state):
        # A flag file could not do better: it would have to be written AFTER the
        # rename it records, so a crash in between leaves it lying. The renames
        # keep this record themselves.
        runs = self._counted(monkeypatch)
        with self._crash_at("version"):
            with pytest.raises(KeyboardInterrupt):
                migrations.run(state)
        assert len(runs) == 1

        assert migrations.run(state) == 1

        assert len(runs) == 1, "the step was handed its own output"
        assert migrations.resolve_version(MigrationStore(state)) == 1

    def test_the_remaining_names_are_what_is_left_in_staging(self, state):
        with self._crash_at("version"):
            with pytest.raises(KeyboardInterrupt):
                migrations.run(state)

        staging = state / ".migrating-v1"
        left = sorted(p.relative_to(staging).as_posix() for p in staging.rglob("*"))
        assert left == ["migrations", "migrations/version"], \
            "already-swapped names should be gone; only the marker remains"

    def test_staging_without_the_marker_is_discarded_not_resumed(self, monkeypatch, state):
        # The marker is written last, so a copy lacking it was abandoned
        # mid-step and says nothing about what is correct. The step must be
        # redone from the live originals.
        runs = self._counted(monkeypatch)
        staging = state / ".migrating-v1"
        (staging / "policies").mkdir(parents=True)
        (staging / "policies" / "junk.json").write_text("{ not json")

        assert migrations.run(state) == 1

        assert len(runs) == 1
        assert not (state / "policies" / "junk.json").exists()
        config = json.loads((state / "config.json").read_text())
        assert (state / "policies" / f"{config['agents']['bob']['id']}.json").exists()

    def test_a_spent_staging_copy_is_cleaned_up(self, state):
        # It holds a copy of config.json, secrets included, and nothing would
        # ever look at it again.
        migrations.run(state)
        spent = state / ".migrating-v1"
        spent.mkdir()
        (spent / "config.json").write_text("{}")

        migrations.run(state)

        assert not spent.exists()


class TestBackupEdgesFoundByReview:
    def test_a_backup_created_but_never_filled_does_not_block_the_next_one(self, state):
        # A crash between `backup.mkdir` and the first rename leaves an EMPTY
        # backup. It reads as pre-versioning, so treating it as an unfinished
        # run's backup would make every later attempt displace the real
        # originals into scratch and delete them.
        real = os.mkdir

        def die_once_created(name, *a, **k):
            real(name, *a, **k)
            if ".pre-migration" in str(name):
                raise KeyboardInterrupt("power cut")

        with mock.patch("os.mkdir", side_effect=die_once_created):
            with pytest.raises(KeyboardInterrupt):
                migrations.run(state)
        assert migrations.backup_path(state).exists()

        migrations.run(state)

        assert (migrations.backup_path(state) / "policies" / "bob.json").exists()

    def test_a_failed_step_does_not_cost_the_existing_backup(self, state, monkeypatch):
        # The superseded backup must not be dropped until the step has actually
        # succeeded, or a failure leaves the user with neither.
        migrations.run(state)
        kept = migrations.backup_path(state) / "policies" / "bob.json"
        before = kept.read_bytes()

        _v0_state(state)
        MigrationStore(state).version_file.unlink()

        def blows_up(store):
            raise RuntimeError("step blew up")

        monkeypatch.setitem(migrations.MIGRATIONS, 1, blows_up)
        with pytest.raises(MigrationError, match="nothing was changed"):
            migrations.run(state)

        assert kept.read_bytes() == before


def test_a_swap_that_rolls_back_cleanly_leaves_no_scratch_behind(state):
    # A successful rollback empties the scratch directory; nothing else ever
    # removes one, and it now sits inside the state directory.
    migrations.run(state)
    MigrationStore(state).version_file.unlink()      # pending again
    real = os.replace

    def fail_the_policies_swap(src, dst, *a, **k):
        if ".migrating-v" in str(src) and str(dst).endswith(os.sep + "policies"):
            raise OSError("read-only")
        return real(src, dst, *a, **k)

    with mock.patch("os.replace", side_effect=fail_the_policies_swap):
        with pytest.raises(MigrationError, match="could not be swapped in"):
            migrations.run(state)

    assert not list(state.glob(".displaced-v*"))
    # Rolled back: the live policies are the ones the FIRST run produced, which
    # are already id-keyed — `bob.json` went into the backup back then.
    config = json.loads((state / "config.json").read_text())
    assert (state / "policies" / f"{config['agents']['bob']['id']}.json").exists()


def test_a_swap_interrupted_between_names_is_resumed_not_refused(state):
    # Crash after config.json is swapped but before policies: the originals are
    # already in the backup and the replacements are waiting in staging. The
    # stranded check must not read "policies missing from live" as unrecoverable
    # — it is pending, and a resume finishes it.
    real = os.replace

    def die_swapping_policies(src, dst, *a, **k):
        if ".migrating-v" in str(src) and str(dst).endswith(os.sep + "policies"):
            raise KeyboardInterrupt("power cut")
        return real(src, dst, *a, **k)

    with mock.patch("os.replace", side_effect=die_swapping_policies):
        with pytest.raises(KeyboardInterrupt):
            migrations.run(state)
    assert not (state / "policies").exists()               # mid-swap
    assert (migrations.backup_path(state) / "policies" / "bob.json").exists()

    assert migrations.run(state) == 1

    config = json.loads((state / "config.json").read_text())
    assert (state / "policies" / f"{config['agents']['bob']['id']}.json").exists()
    assert (migrations.backup_path(state) / "policies" / "bob.json").exists()


def test_the_backup_is_completed_by_the_resume_not_split_in_two(state):
    # The interrupted run put config.json in the backup; the resume displaces
    # policies. Both belong in the same directory, or the backup is not a usable
    # copy of anything.
    real = os.replace

    def die_swapping_policies(src, dst, *a, **k):
        if ".migrating-v" in str(src) and str(dst).endswith(os.sep + "policies"):
            raise KeyboardInterrupt("power cut")
        return real(src, dst, *a, **k)

    with mock.patch("os.replace", side_effect=die_swapping_policies):
        with pytest.raises(KeyboardInterrupt):
            migrations.run(state)
    migrations.run(state)

    kept = migrations.backup_path(state)
    assert sorted(p.name for p in kept.iterdir()) == ["config.json", "policies"]
    assert not list(state.glob(".displaced-v*"))


def test_a_multi_step_run_backs_up_the_original_not_the_intermediate(monkeypatch, state):
    # Only the first step's displaced files are the state the user was running.
    # Step 2 displaces what step 1 produced — a schema no build wants and no one
    # would restore. Without the distinction the backup ends up holding it.
    def m0002(store):
        doc = store.read_config()
        doc["touched_by_step_2"] = True
        store.write_config(doc)

    monkeypatch.setitem(migrations.MIGRATIONS, 2, m0002)
    monkeypatch.setattr(migrations, "CURRENT_VERSION", 2)

    assert migrations.run(state) == 2

    kept = migrations.backup_path(state)
    backed_up = json.loads((kept / "config.json").read_text())
    assert "touched_by_step_2" not in backed_up, "backed up the intermediate"
    assert not backed_up["agents"]["bob"].get("id"), "backed up step 1's output"
    assert (kept / "policies" / "bob.json").exists()        # the name-keyed original
    assert not list(state.glob(".displaced-v*"))            # scratch cleaned up
    assert migrations.resolve_version(MigrationStore(state)) == 2


def test_a_rolled_back_swap_is_redone_not_resumed(state):
    # `_swap` consumes a name out of staging as it swaps it in, so `_resumable`
    # reads "not in staging" as "already live". A rollback makes that false: the
    # name is live again, but as the ORIGINAL. Resuming would skip it and leave
    # it unmigrated — or, if every name was rolled back, move only the marker
    # and stamp the new version over old data.
    real = os.replace

    def fail_after_config_is_in(src, dst, *a, **k):
        # config.json has been swapped by now; this is the policies displacement
        if str(src).endswith(os.sep + "policies") and ".pre-migration" in str(dst):
            raise OSError(5, "EIO")
        return real(src, dst, *a, **k)

    with mock.patch("os.replace", side_effect=fail_after_config_is_in):
        with pytest.raises(MigrationError, match="could not be swapped in"):
            migrations.run(state)

    assert not (state / ".migrating-v1").exists(), "kept an invalid resume source"
    assert json.loads((state / "config.json").read_text())["agents"]["bob"].get("id") is None

    assert migrations.run(state) == 1

    config = json.loads((state / "config.json").read_text())
    agent_id = config["agents"]["bob"]["id"]
    assert agent_id, "config.json was skipped by a resume"
    doc = json.loads((state / "policies" / f"{agent_id}.json").read_text())
    assert doc["permissions"], "grants lost"
    assert (migrations.backup_path(state) / "policies" / "bob.json").exists()


def test_the_marker_is_replaced_atomically_never_absent(monkeypatch, state):
    # Moving the old marker aside and renaming the new one in leaves a window
    # where the state carries NO marker, which reads as pre-versioning. A crash
    # there is unrecoverable by retrying: the state holds vN data while
    # reporting v0, so every retry re-runs steps already applied, against the
    # data they produced, and jams on a backup it cannot overwrite.
    def m0002(store):
        doc = store.read_config()
        doc["v2"] = True
        store.write_config(doc)

    monkeypatch.setitem(migrations.MIGRATIONS, 2, m0002)
    monkeypatch.setattr(migrations, "CURRENT_VERSION", 2)
    migrations.run(state)                       # settle at v2 with a real marker

    seen = []
    real = os.replace

    def watch(src, dst, *a, **k):
        if str(dst).endswith(os.sep + "version") and ".migrating-v" not in str(dst):
            seen.append(migrations.resolve_version(MigrationStore(state)))
        return real(src, dst, *a, **k)

    MigrationStore(state).version = 1           # pretend v2 is pending again
    with mock.patch("os.replace", side_effect=watch):
        migrations.run(state)

    assert seen == [1], "the live marker was read as something other than v1"
    assert migrations.resolve_version(MigrationStore(state)) == 2
    assert (migrations.backup_path(state) / "migrations" / "version").exists(), \
        "the superseded marker was not kept with the rest of the backup"


def test_a_crash_committing_the_marker_of_a_later_step_still_recovers(monkeypatch, state):
    # The regression this guards: with the marker moved aside first, a crash
    # here left v2 data reporting v0 and every retry failed identically.
    def m0002(store):
        doc = store.read_config()
        doc["v2"] = True
        store.write_config(doc)

    monkeypatch.setitem(migrations.MIGRATIONS, 2, m0002)
    monkeypatch.setattr(migrations, "CURRENT_VERSION", 2)
    real = os.replace

    def die_committing_v2(src, dst, *a, **k):
        if (".migrating-v2" in str(src) and ".migrating-v" not in str(dst)
                and str(dst).endswith(os.sep + "version")):
            raise KeyboardInterrupt("power cut")
        return real(src, dst, *a, **k)

    with mock.patch("os.replace", side_effect=die_committing_v2):
        with pytest.raises(KeyboardInterrupt):
            migrations.run(state)
    assert migrations.resolve_version(MigrationStore(state)) == 1, "marker went missing"

    assert migrations.run(state) == 1

    assert migrations.resolve_version(MigrationStore(state)) == 2
    assert json.loads((state / "config.json").read_text())["v2"] is True
    assert not list(state.glob(".displaced-v*"))
    assert not list(state.glob(".migrating-v*"))


def test_grants_still_resolve_through_the_product_after_migrating(tmp_path, monkeypatch):
    """The acceptance criterion: what m0001 writes is what the gateway reads.

    Every other test checks the files. This one loads them back through
    `ConfigManager` and `PermissionEngine` the way a tool call does, so a
    migration that produced well-formed-but-wrong documents would be caught.
    """
    import steerholm.config as _c
    from steerholm.permissions import PermissionEngine

    state = tmp_path / "state"
    (state / "policies").mkdir(parents=True)
    (state / "config.json").write_text(json.dumps({
        "servers": {n: {"name": n, "command": "uvx x", "url": "", "env": {},
                        "server_type": "stdio"} for n in ("git", "db")},
        "agents": {n: {"name": n, "key_prefix": "steer_sk_x..."} for n in ("bob", "eve")},
    }))
    (state / "policies" / "bob.json").write_text(json.dumps({
        "agent_name": "bob",
        "permissions": {
            "git": [{"name": "git_log", "policies": []}],
            "db": [{"name": "query", "policies": [
                {"arg_name": "mode", "match_type": "glob", "pattern": "read*"}]}],
        }}))
    (state / "policies" / "eve.json").write_text(json.dumps({
        "agent_name": "eve", "permissions": {"git": [{"name": "git_status", "policies": []}]}}))

    migrations.run(state)

    monkeypatch.setattr(_c, "CONFIG_DIR", state)
    monkeypatch.setattr(_c, "CONFIG_FILE", state / "config.json")
    monkeypatch.setattr(_c, "POLICIES_DIR", state / "policies")
    manager = _c.ConfigManager()

    def allowed(agent, server, tool, args):
        policy = manager.load_policy(agent)
        assert policy is not None, f"{agent} lost its policy"
        try:
            PermissionEngine(policy).check_permission(
                manager.get_server(server).id, tool, args, server)
            return True
        except Exception:
            return False

    assert allowed("bob", "git", "git_log", {})
    assert allowed("bob", "db", "query", {"mode": "readonly"})
    assert not allowed("bob", "db", "query", {"mode": "write"})   # arg policy kept
    assert not allowed("bob", "git", "git_push", {})              # not granted
    assert allowed("eve", "git", "git_status", {})
    assert not allowed("eve", "db", "query", {})                  # never granted
    assert not allowed("eve", "git", "git_log", {})               # bob's, not eve's


def test_a_truncated_policy_names_the_file_it_could_not_read(state):
    # 0.1.x wrote policies with a plain truncate-and-write, so a crash or a full
    # disk mid-save leaves one of these. The bare decoder message says nothing
    # about which of an install's policies to go and look at.
    (state / "policies" / "bob.json").write_text('{"agent_name": "bob", "permi')

    with pytest.raises(MigrationError) as e:
        migrations.run(state)

    assert "bob.json" in str(e.value)
    assert "not valid JSON" in str(e.value)
    assert ".migrating-v" not in str(e.value)          # names the live path
    assert (state / "policies" / "bob.json").exists()  # nothing was changed
    assert migrations.resolve_version(MigrationStore(state)) == 0
