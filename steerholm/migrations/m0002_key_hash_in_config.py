"""Agent key hashes move from the OS keyring into `config.json`.

Schema 1 -> 2. First shipped in: (unreleased)

The keyring could not be relied on. It has no backend on a headless box, so
`holm add agent` failed outright with a message from the keyring library telling
the user to install a third-party package; and when a backend exists but is
locked — an SSH session where the login keyring was never unlocked — reads raise
and a valid key silently stops working while every CLI surface still reports the
agent as healthy.

What it held was a bcrypt hash: a verifier, not a secret, and not reversible
into a key. It sat behind a lock while `config.json`, in the same directory,
holds every `--env` value in plaintext. The strongest credentials were already
in the file; only the weakest was in the keyring.

    config.json  {"agents": {"bob": {..., "key_hash": "$2b$..."}}}

Reading the keyring is the only thing this step needs it for, and reading is
safe: the engine's staging and rollback cover the state directory, so a step
must not MUTATE anything outside it. Entries are therefore left in place — which
also means a user who migrates while the keyring is locked can unlock it and run
again, and the skipped agents will be picked up then.

This step is why `keyring` stays a dependency and why the frozen binaries still
bundle `keyrings.alt` (see .github/workflows/build.yml). On a headless box
keyring selects keyrings.alt's PlaintextKeyring automatically — with no D-Bus,
SecretService is not viable — so that is where those installs put their hashes,
and nothing else can read that file. Both can be dropped once no un-migrated
install remains, which is a judgement about the population rather than something
this code can determine; until then the dependency is load-bearing, not
vestigial.
"""
import logging

logger = logging.getLogger("steerholm.migrations")


def migrate(store) -> None:
    config = store.read_config()
    agents = (config or {}).get("agents") or {}
    if not agents:
        return

    try:
        import keyring
    except Exception as e:                      # pragma: no cover - import guard
        logger.warning("Cannot read the keyring (%s); every agent needs its key "
                       "reissued with `holm rotate agent <name>`.", e)
        store.write_config(config)
        return

    carried, skipped = [], []
    for name, agent in agents.items():
        if not isinstance(agent, dict) or agent.get("key_hash"):
            continue                            # already carried; re-runnable
        try:
            hashed = keyring.get_password("steerholm", name)
        except Exception as e:
            logger.warning("Could not read the keyring entry for %r: %s", name, e)
            skipped.append(name)
            continue
        if hashed:
            agent["key_hash"] = hashed
            carried.append(name)
        else:
            skipped.append(name)

    store.write_config(config)

    if carried:
        logger.info("Carried the key hash for %d agent(s) out of the keyring.",
                    len(carried))
    if skipped:
        # Not an error: the entries are untouched, so unlocking the keyring and
        # running again picks them up. Only if that never happens does the key
        # have to be reissued.
        logger.warning(
            "No key hash for %s. If the keyring is locked, unlock it and run "
            "`holm migrate` again; otherwise reissue with `holm rotate agent "
            "<name>` — until then those agents cannot authenticate.",
            ", ".join(repr(n) for n in skipped))
