"""Grants key on immutable ids rather than reusable names.

Schema 0 -> 1. First shipped in: (unreleased)

Names are reusable: remove an agent or a server and add another under the same
name, and it is a different principal. A grant stored under a name could
therefore outlive what it was written for and silently attach to whatever took
the name next. Grants now key on the ids minted at creation.

On disk that means:

    policies/<agent name>.json        ->  policies/<agent id>.json
    {"agent_name": "bob",                 {"agent_id": "agt_...",
     "permissions": {"git": [...]}}        "permissions": {"srv_...": [...]}}

Entities created before ids existed have `id: null`, so this backfills them
first. Derived from the name rather than random so that two processes running
this at the same time produce the same result.
"""
import hashlib
import logging
import os
import re
import unicodedata
from typing import Any, Dict

from .store import StateError, restrict

# Ids this build mints. A policy already named with one has been through this
# step before — the marker just did not land.
_ID = re.compile(r"(?:agt_|srv_)[0-9a-f]{16}")

logger = logging.getLogger("steerholm.migrations")

# Deliberately NO name validation here. 0.1.x accepted any agent name — it had
# no validation at all — so `2024-bot`, `my agent` and `café` are real names on
# real installs. Refusing them would strip those agents' grants while reporting
# success. Nothing needs the check either: the name only ever comes from the
# stem of a file already inside `policies/`, so it cannot escape the directory,
# and the path that is written comes from the id, which the store validates.


def _derived_id(prefix: str, name: str) -> str:
    digest = hashlib.sha256(f"{prefix}{name}".encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:16]}"


def migrate(store) -> None:
    config = store.read_config()
    if config.get("identities") is not None and config.get("agents") is None:
        # Pre-0.1.0 named this key `identities`; 0.1.x carried a shim that has
        # since been removed. Refusing is deliberate — support for those states
        # was dropped on purpose — but refusing LOUDLY is the point. Migrating
        # anyway would find no agents, file every policy under `unmigrated/`,
        # stamp the marker current and report success, and the config that came
        # out the other side does not load at all: `Config` forbids unknown
        # keys, so every server and agent would silently disappear behind a
        # warning.
        raise StateError(
            f"{store.config_file} still uses the pre-0.1.0 'identities' key. "
            "This build migrates from 0.1.x onwards. Rename that key to "
            "'agents' and run this again."
        )
    agent_ids = _backfill(config.get("agents") if config else None, "agt_")
    server_ids = _backfill(config.get("servers") if config else None, "srv_")
    if config:
        store.write_config(config)

    # Run the policy pass even when the config is empty or has no agents. The
    # step must not finish with name-keyed documents still in `policies/` while
    # the marker says they are id-keyed; anything that cannot be converted is
    # set aside rather than left looking current.
    for path in store.policy_files():
        _rekey_policy(store, path, agent_ids, server_ids)


def _backfill(entities: Dict[str, Any], prefix: str) -> Dict[str, str]:
    """Give ids to entities that predate them. Returns name -> id."""
    ids = {}
    for name, entity in (entities or {}).items():
        if not entity.get("id"):
            entity["id"] = _derived_id(prefix, name)
        ids[name] = entity["id"]
    return ids


def _resolve_agent(agent_ids, name: str):
    """Map a policy filename back to the config key it belongs to.

    Byte equality is not enough. A normalising filesystem — HFS+, and any
    volume that stores names decomposed — hands back `café` as NFD while the
    config, being JSON, still holds the NFC the name was typed in. Those are
    the same text, so they must resolve to the same agent; matching on bytes
    alone would file that agent's grants under `unmigrated/` and report
    success.

    Only the canonical forms are tried. NFKC/NFKD fold characters that are
    genuinely different text, which could resolve a filename onto some other
    agent entirely. Returns None when nothing matches, and None when more than
    one distinct agent does — an ambiguous name is not one to guess at.
    """
    if name in agent_ids:
        return name
    matches = {form for form in (unicodedata.normalize("NFC", name),
                                 unicodedata.normalize("NFD", name))
               if form in agent_ids}
    return matches.pop() if len(matches) == 1 else None


def _set_aside(store, path, why: str) -> None:
    """Move a policy this step cannot convert out of `policies/`.

    Leaving it would end the step with a schema-0 document sitting in a
    directory the marker declares schema 1, and a later migration iterating
    `policy_files()` would read it as though it were current.
    """
    unmigrated = store.policies_dir / "unmigrated"
    unmigrated.mkdir(parents=True, exist_ok=True)
    restrict(unmigrated, 0o700)
    target = unmigrated / path.name
    logger.warning("Setting %s aside (%s); it is now at %s.", path.name, why, target)
    os.replace(path, target)


def _rekey_policy(store, path, agent_ids, server_ids) -> None:
    """Rewrite one `<agent name>.json` as `<agent id>.json`, keyed by server id."""
    filename_name = store.policy_name(path)
    if _ID.fullmatch(filename_name):
        # This step's own output. Required, not defensive: `_swap` renames the
        # marker last, so a crash after `policies/` lands leaves this data
        # converted and the version still reading 0 — and the retry feeds it
        # back here. Without this the file reads as "naming no agent in the
        # config" and every live grant goes to `unmigrated/`. See MIGRATIONS
        # in __init__.py.
        return

    agent_name = _resolve_agent(agent_ids, filename_name)

    if agent_name is None:
        # A policy file naming no agent in the config. It could never have
        # granted anything — the gateway resolves policies from the config — so
        # there is nothing to carry forward and no id to carry it under. Set it
        # aside so the directory does not keep a schema-0 document that a later
        # step would read as current.
        _set_aside(store, path, "no agent of that name in the config")
        return
    agent_id = agent_ids[agent_name]
    doc = store.read_policy(path)
    # The filename says one agent, the document says another, and both are real.
    # A case-insensitive filesystem is how this happens: 0.1.x wrote agents `CI`
    # and `ci` to one and the same file, so whichever wrote last owns the
    # contents while the directory listing still yields the other's name.
    # Guessing here would hand one agent's grants to a different principal —
    # the exact transfer this step exists to make impossible.
    claimed = doc.get("agent_name")
    if isinstance(claimed, str):
        claimed_name = _resolve_agent(agent_ids, claimed)
        if claimed_name is not None and claimed_name != agent_name:
            _set_aside(store, path, f"names {agent_name!r} but claims {claimed!r}")
            return

    raw = doc.get("permissions") or {}

    permissions = {}
    for server_name, tools in raw.items():
        server_id = server_ids.get(server_name)
        if server_id is None:
            # Names a server that no longer exists, so there is no id to point
            # it at. It could never have matched anything.
            logger.warning("Dropping grant for unknown server %r held by %r",
                           server_name, agent_name)
            continue
        permissions[server_id] = tools

    store.write_policy(agent_id, {"agent_id": agent_id, "permissions": permissions})
    path.unlink()
