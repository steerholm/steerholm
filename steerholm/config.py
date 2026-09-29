import os
import re
import sys
import json
import logging
import secrets
import string
from pathlib import Path
from typing import Optional, List, Tuple
import bcrypt
from .models import (
    AuditSettings, Config, Server, Agent, AgentPolicy, ToolPermission,
    ArgumentPolicy, ServerType,
)

logger = logging.getLogger("steerholm.config")


def _get_config_dir() -> Path:
    override = os.environ.get("STEERHOLM_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "steerholm"
    return Path.home() / ".steerholm"


CONFIG_DIR = _get_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
POLICIES_DIR = CONFIG_DIR / "policies"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4767

# Its own file rather than a field in config.json: not user-facing
# configuration, and it would otherwise show up in every dump of that model.
#
# Resolved at call time, not bound at import: CONFIG_DIR is re-pointed in tests
# and a constant captured here would keep addressing the directory it had when
# the module loaded.
def control_token_path() -> Path:
    return CONFIG_DIR / "control-token"


def read_control_token() -> Optional[str]:
    """The loopback control-plane token, or None if it has not been created.

    Everything except daemon startup reads: the CLI only reaches here after
    `_daemon_up()`, so the daemon has already created it, and the verifier is
    checking against whatever is on disk. Minting one here instead would be the
    worst answer available — a token nobody was issued, so the CLI would present
    a value the daemon rejects and a missing file would surface as a silent 401.
    """
    path = control_token_path()
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def get_or_create_control_token() -> str:
    """Create the control token if absent and return it. Daemon startup only.

    Stored raw, unlike agent keys: the CLI has to present the value, so both
    ends need the real thing. Owner-only, the same protection `config.json`
    gets for the `--env` values it already holds.

    Written via a temp file and a rename so a reader never catches it
    half-written. Two daemons racing to start would both write, and that is
    harmless: nothing caches the token, so whichever value lands is what every
    later read — verifier and CLI alike — agrees on.
    """
    existing = read_control_token()
    if existing:
        return existing
    token = "steer_ctl_" + "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(32)
    )
    path = control_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"control-token.{os.getpid()}.tmp")
    try:
        tmp.write_text(token + "\n", encoding="utf-8")
        _restrict(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return token


def _restrict(path, mode: int) -> None:
    """Restrict a config file or directory to the owner only. No-op on Windows,
    where per-user AppData already isolates it via ACLs and POSIX modes don't map."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


# Env var name rule shared by Kubernetes and Docker's build/Compose layer:
# letters, digits, '_', '-', '.', not starting with a digit. Every name it
# accepts is valid on every OS we support, so a config stays portable.
_ENV_KEY_RE = re.compile(r"[-._a-zA-Z][-._a-zA-Z0-9]*")


# Names end up as path segments (policies/<agent id>.json) and as audit-log
# identifiers, so they have to be inert in both. Letters, digits, '_', '-', '.',
# starting with a letter or '_' — the same shape as the env-var rule, and it
# excludes the separators and '..' that would let a name escape its directory.
_NAME_RE = re.compile(r"[_a-zA-Z][-._a-zA-Z0-9]*")
_ID_PREFIXES = ("agt_", "srv_")


def validate_entity_name(kind: str, name: str) -> None:
    """Reject agent/server names that are unsafe as a filename or ambiguous with
    an id.

    Two hazards. A name containing a path separator or '..' can escape the
    policies directory wherever a name is still used to build a path. A name
    shaped like an id ('agt_<hex>') can collide with another entity's real id,
    which would let one principal's policy file be mistaken for another's.
    """
    if not name:
        raise ValueError(f"{kind} name cannot be empty.")
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid {kind.lower()} name {name!r}: names must consist of letters, "
            "digits, '_', '-', or '.', and must start with a letter or '_'."
        )
    # Case-folded: macOS and Windows filesystems are case-insensitive, so
    # `AGT_<hex>` would resolve to the same policy file as a real `agt_<hex>` id.
    if name.lower().startswith(_ID_PREFIXES):
        raise ValueError(
            f"Invalid {kind.lower()} name {name!r}: {'/'.join(_ID_PREFIXES)} is "
            "reserved for ids, which must not be confusable with names."
        )


def _new_server_id() -> str:
    """Mint an immutable server id, set once at creation, so the audit log can
    tell a re-added name apart from the server it replaced."""
    return "srv_" + secrets.token_hex(8)


def _new_agent_id() -> str:
    """Mint an immutable agent id, set once at creation and kept across key
    rotation, so the audit log can tell a deleted-then-recreated name apart from
    the original (same name, different principal)."""
    return "agt_" + secrets.token_hex(8)


def validate_env_key(key: str) -> None:
    """Reject env var names that aren't portable across platforms.

    Uses the same rule as Kubernetes and Docker's build/Compose layer — a name is
    letters, digits, '_', '-', or '.', and must not start with a digit — so every
    accepted name is valid on every supported OS. It rejects platform-specific
    oddities (spaces, parentheses, '+', '#', ...) that one OS might tolerate but
    that break portability and shell/tooling use. Since the daemon already inherits
    its full environment, '--env' only declares app config and secrets, which use
    conventional names, so this costs nothing in practice.
    """
    if not key:
        raise ValueError("Environment variable name cannot be empty.")
    if not _ENV_KEY_RE.fullmatch(key):
        raise ValueError(
            f"Invalid environment variable name {key!r}: names must consist of "
            "letters, digits, '_', '-', or '.', and must not start with a digit."
        )


class SchemaError(Exception):
    """The on-disk state is a schema this build cannot safely read or write."""


# sysexits.h EX_CONFIG, the exit code for a SchemaError from anything a service
# manager runs. Named in the systemd unit's RestartPreventExitStatus: only
# `holm migrate` can fix a schema mismatch, so restarting just loops every
# RestartSec. Plain failure here is what that used to do.
EX_CONFIG = 78


class ConfigManager:
    def __init__(self):
        self._ensure_dirs()
        self._stamp_if_new()
        self.config = self._load_config()

    @staticmethod
    def _stamp_if_new() -> None:
        """Mark brand-new state as current. It has nothing to bring forward.

        Without this the first `holm add server` would write a config with no
        version marker, and every later command would read it as schema 0 and
        refuse to run.
        """
        from . import migrations

        store = migrations.MigrationStore(CONFIG_DIR)
        if store.version_file.exists():
            return
        if CONFIG_FILE.exists() or store.policy_files():
            # There is state here, and no marker, so it predates versioning and
            # needs migrating. Stamping it current would skip that permanently
            # and leave every policy unreadable. Policies count on their own:
            # a crash between the config and policy renames can leave exactly
            # that shape, and it is the one case this must not mistake for new.
            return
        store.version = migrations.CURRENT_VERSION

    @staticmethod
    def verify_schema() -> None:
        """Refuse to run against state this build does not understand.

        Verifies; never migrates. Deliberately not called from `__init__`: the
        CLI builds a manager at import, so refusing there would take down `holm
        migrate` — the one command that fixes it — along with everything else.
        Callers invoke it once, before the config is used.
        """
        from . import migrations

        store = migrations.MigrationStore(CONFIG_DIR)
        if not CONFIG_FILE.exists() and not store.policy_files() \
                and not store.version_file.exists():
            return                      # genuinely empty: nothing to be behind
        try:
            version = migrations.resolve_version(store)
        except migrations.store.StateError as e:
            raise SchemaError(str(e)) from e
        if version == migrations.CURRENT_VERSION:
            return
        if version > migrations.CURRENT_VERSION:
            raise SchemaError(
                f"{CONFIG_DIR} is schema v{version}; this build of Steerholm "
                f"understands v{migrations.CURRENT_VERSION}. Upgrade Steerholm."
            )
        raise SchemaError(
            f"{CONFIG_DIR} is schema v{version}; this build of Steerholm expects "
            f"v{migrations.CURRENT_VERSION}. Reinstall with the official "
            "installer, which migrates it:\n"
            "  curl -fsSL https://steerholm.ai/install.sh | bash"
        )

    def _ensure_dirs(self):
        # The config holds server secrets (--env), agent policies, and grants, so
        # keep it owner-only. Re-applying on every run hardens a pre-existing
        # install (e.g. a v0.1.0 config created world-readable) the first time the
        # updated binary runs — the daemon restart on update triggers this — and
        # covers both the dirs and any files already on disk.
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        POLICIES_DIR.mkdir(parents=True, exist_ok=True)
        _restrict(CONFIG_DIR, 0o700)
        _restrict(POLICIES_DIR, 0o700)
        if CONFIG_FILE.exists():
            _restrict(CONFIG_FILE, 0o600)
        for policy in POLICIES_DIR.glob("*.json"):
            _restrict(policy, 0o600)

    def _load_config(self) -> Config:
        if not CONFIG_FILE.exists():
            return Config()
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            return Config(**data)
        except Exception as e:
            print(f"Warning: Could not load config: {e}")
            return Config()

    def save_config(self):
        with open(CONFIG_FILE, "w") as f:
            f.write(self.config.model_dump_json(indent=2))
        _restrict(CONFIG_FILE, 0o600)

    def reload(self):
        self.config = self._load_config()

    # --- Server Management ---
    def add_server(self, name: str, command: str = None, url: str = None,
                   env: dict = None) -> Server:
        """Dock a server. Provide command (stdio) or url (http), not both."""
        validate_entity_name("Server", name)
        if name in self.config.servers:
            raise ValueError(f"Server '{name}' already exists.")
        if command and url:
            raise ValueError("Provide command or url, not both.")
        if not command and not url:
            raise ValueError("Provide command (stdio) or url (http).")
        if env and url:
            raise ValueError(
                "Environment variables apply to stdio servers (--command), "
                "not remote (--url) servers."
            )
        for key in (env or {}):
            validate_env_key(key)

        if command:
            server = Server(name=name, id=_new_server_id(), command=command,
                            env=env or {}, server_type=ServerType.stdio)
        else:
            server = Server(name=name, id=_new_server_id(), url=url,
                            server_type=ServerType.http)

        self.config.servers[name] = server
        self.save_config()
        return server

    def modify_server(self, name: str, command: str = None, url: str = None,
                      env: dict = None, unset: List[str] = None) -> Server:
        """Change an existing server in place, keeping its id and its grants.

        Only the fields given are changed; `env` merges into what is already
        there and `unset` removes keys. Switching transport clears the state that
        belongs to the old one — a URL has no launch command or environment.
        """
        if name not in self.config.servers:
            raise ValueError(f"Server '{name}' not found.")
        if command and url:
            raise ValueError("Provide command or url, not both.")
        if url and (env or unset):
            raise ValueError(
                "Environment variables apply to stdio servers (--command), "
                "not remote (--url) servers."
            )
        existing = self.config.servers[name]
        if url is None and (env or unset) and existing.server_type == ServerType.http:
            raise ValueError(
                "Environment variables apply to stdio servers (--command), "
                f"and '{name}' is a remote (--url) server."
            )

        merged = dict(existing.env)
        for key in (unset or []):
            merged.pop(key, None)
        for key, value in (env or {}).items():
            validate_env_key(key)
            merged[key] = value

        if url is not None:                      # -> remote; stdio state goes away
            server = Server(name=name, id=existing.id, url=url,
                            server_type=ServerType.http)
        elif command is not None:                # -> stdio (or a new command)
            server = Server(name=name, id=existing.id, command=command,
                            env=merged, server_type=ServerType.stdio)
        else:                                    # env-only change, transport kept
            server = Server(name=name, id=existing.id, command=existing.command,
                            url=existing.url, env=merged,
                            server_type=existing.server_type)

        self.config.servers[name] = server
        self.save_config()
        return server

    # --- Audit log retention ---
    def audit_kwargs(self) -> dict:
        """The audit settings as EventLog constructor/configure arguments.
        Segment size is fixed (see AuditSettings), so it is not included."""
        audit = self.config.audit
        return {
            "max_files": audit.max_files,
            "max_age_days": audit.max_age_days,
        }

    def set_audit_settings(self, max_files: int = None,
                           max_age_days: int = None) -> AuditSettings:
        """Update audit-log retention. Only the values given are changed."""
        audit = self.config.audit
        if max_files is not None:
            if max_files < 0:
                raise ValueError("Maximum files cannot be negative (0 = no limit).")
            audit.max_files = max_files
        if max_age_days is not None:
            if max_age_days < 0:
                raise ValueError("Maximum age cannot be negative (0 = no limit).")
            audit.max_age_days = max_age_days
        self.save_config()
        return audit

    def remove_server(self, name: str) -> List[str]:
        """Remove a server and every grant on it. Returns the agents affected.

        Grants are cascaded for the same reason removing an agent deletes its
        policy: a name can be reused, so a grant that outlived its server would
        silently attach to whatever is added under that name next.
        """
        if name not in self.config.servers:
            raise ValueError(f"Server '{name}' not found.")
        # Resolve the id before the server is gone; the grants are keyed on it.
        server_id = self._server_id(name)
        del self.config.servers[name]
        self.save_config()

        affected = []
        for agent_name in list(self.config.agents):
            policy = self.load_policy(agent_name)
            if policy and server_id in policy.permissions:
                del policy.permissions[server_id]
                self.save_policy(agent_name, policy)
                affected.append(agent_name)
        return affected

    def agents_with_grants(self, server_name: str) -> List[str]:
        """Agents holding any grant on a server, so a change can say who it affects."""
        server_id = self._server_id(server_name)
        if server_id is None:
            return []
        names = []
        for agent_name in self.config.agents:
            policy = self.load_policy(agent_name)
            if policy and server_id in policy.permissions:
                names.append(agent_name)
        return names

    def get_server(self, name: str) -> Optional[Server]:
        return self.config.servers.get(name)

    def list_servers(self) -> List[Server]:
        return list(self.config.servers.values())

    # --- Agent Management ---
    def _generate_access_key(self) -> Tuple[str, str]:
        """Mint a fresh access key. Returns (raw key, bcrypt hash).

        The raw key is returned once, at creation or rotation, and never
        stored; only the hash is kept.
        """
        alphabet = string.ascii_letters + string.digits
        token = "".join(secrets.choice(alphabet) for _ in range(32))
        access_key = f"steer_sk_{token}"
        return access_key, bcrypt.hashpw(access_key.encode(), bcrypt.gensalt()).decode()

    def add_agent(self, name: str) -> str:
        """Create an agent and generate its access key.
        Returns the key. Only available at creation time; only a hash is kept."""
        validate_entity_name("Agent", name)
        if name in self.config.agents:
            raise ValueError(f"Agent '{name}' already exists.")
        access_key, key_hash = self._generate_access_key()
        self.config.agents[name] = Agent(
            name=name, id=_new_agent_id(), key_prefix=access_key[:15] + "...",
            key_hash=key_hash,
        )
        self.save_config()
        return access_key

    def rotate_agent_key(self, name: str) -> str:
        """Generate a new access key for an existing agent, keeping its grants.
        Returns the new key; the old one stops working immediately."""
        if name not in self.config.agents:
            raise ValueError(f"Agent '{name}' not found.")
        access_key, key_hash = self._generate_access_key()
        # Rotation is a new credential for the same principal — keep the id (and a
        # legacy agent's absent id stays absent; it's minted only at creation).
        self.config.agents[name] = Agent(
            name=name, id=self.config.agents[name].id,
            key_prefix=access_key[:15] + "...", key_hash=key_hash,
        )
        self.save_config()
        return access_key

    def get_agent(self, name: str) -> Optional[Agent]:
        return self.config.agents.get(name)

    def remove_agent(self, name: str):
        """Remove an agent and its policy. The key hash goes with the entry."""
        if name not in self.config.agents:
            raise ValueError(f"Agent '{name}' not found.")
        stale = []
        try:
            stale.append(self.policy_path_for(name))
        except ValueError:
            pass          # no id: nothing id-keyed to remove
        # A policy file left over from the name-keyed layout would otherwise
        # be grafted onto whoever next takes this name. Only follow the name
        # when it is safe to build a path from.
        try:
            validate_entity_name("Agent", name)
        except ValueError:
            logger.warning(
                "Not removing a name-keyed policy for %r: the name is not "
                "safe to use as a filename.", name,
            )
        else:
            stale.append(POLICIES_DIR / f"{name}.json")

        del self.config.agents[name]
        self.save_config()
        for policy_path in stale:
            if policy_path.exists():
                try:
                    policy_path.unlink()
                except OSError:
                    pass

    def list_agents(self) -> list:
        return list(self.config.agents.values())

    # --- Policy Management ---
    def _agent_id(self, agent_name: str) -> Optional[str]:
        agent = self.config.agents.get(agent_name)
        return agent.id if agent else None

    def _server_id(self, server_name: str) -> Optional[str]:
        server = self.config.servers.get(server_name)
        return server.id if server else None

    def policy_path_for(self, agent_name: str) -> Path:
        """Where this agent's policy lives, resolved from the config.

        Always from the config's id, never from an `agent_id` read out of a
        policy file: if the two disagree, a write keyed on the file's own field
        would land somewhere else and silently leave the real grant in place.
        """
        agent_id = self._agent_id(agent_name)
        if agent_id is None:
            raise ValueError(f"Agent '{agent_name}' not found.")
        return POLICIES_DIR / f"{agent_id}.json"

    def create_policy(self, agent_name: str) -> AgentPolicy:
        agent_id = self._agent_id(agent_name)
        if agent_id is None:
            if agent_name in self.config.agents:
                raise ValueError(
                    f"Agent '{agent_name}' has no id, so it cannot hold grants. "
                    f"Remove and re-add it with 'holm add agent {agent_name}'."
                )
            raise ValueError(f"Agent '{agent_name}' not found.")
        policy = AgentPolicy(agent_id=agent_id, permissions={})
        self.save_policy(agent_name, policy)
        return policy

    def save_policy(self, agent_name: str, policy: AgentPolicy):
        """Persist a policy. Takes the agent NAME so the path comes from the
        config rather than from the document being written."""
        path = self.policy_path_for(agent_name)
        with open(path, "w") as f:
            f.write(policy.model_dump_json(indent=2))
        _restrict(path, 0o600)

    def grant_permission(self, agent_name: str, server_name: str,
                         tool: str = "*", arg_policies: List[str] = None):
        """Grant a tool permission to an agent on a server.
        arg_policies: list of 'arg=pattern' or 'arg=re:pattern' strings."""
        if agent_name not in self.config.agents:
            raise ValueError(f"Agent '{agent_name}' not found.")
        server_id = self._server_id(server_name)
        if server_id is None:
            # A grant records the server's id, so there is nothing to point at.
            raise ValueError(
                f"Server '{server_name}' not found. Add it first with "
                f"'holm add server {server_name}'."
            )
        policies = []
        for arg_str in (arg_policies or []):
            if "=" not in arg_str:
                raise ValueError(f"Invalid argument policy format: '{arg_str}'. Use arg=pattern or arg=re:pattern")
            key, pattern = arg_str.split("=", 1)
            if pattern.startswith("re:"):
                match_type = "regex"
                pattern = pattern[3:]
            else:
                match_type = "glob"
            policies.append(ArgumentPolicy(arg_name=key, match_type=match_type, pattern=pattern))

        policy = self.load_policy(agent_name)
        if not policy:
            policy = self.create_policy(agent_name)

        if server_id not in policy.permissions:
            policy.permissions[server_id] = []

        policy.permissions[server_id].append(ToolPermission(name=tool, policies=policies))
        self.save_policy(agent_name, policy)

    def revoke_permission(self, agent_name: str, server_name: str,
                          tool: str = None) -> bool:
        """Remove access an agent has to a server. With `tool`, drop only grants
        whose tool pattern matches it exactly; without, drop the whole server.
        Returns True if anything was removed."""
        if agent_name not in self.config.agents:
            raise ValueError(f"Agent '{agent_name}' not found.")
        policy = self.load_policy(agent_name)
        if not policy:
            return False
        server_id = self._server_id(server_name)
        if server_id is None and server_name in policy.permissions:
            # The server is gone but its grant outlived the cascade. `holm show
            # agent` prints such a grant as a raw id, so accept that id here —
            # otherwise it names something no command can address.
            server_id = server_name
        if server_id is None or server_id not in policy.permissions:
            return False

        if tool is None:
            del policy.permissions[server_id]
            self.save_policy(agent_name, policy)
            return True

        remaining = [p for p in policy.permissions[server_id] if p.name != tool]
        if len(remaining) == len(policy.permissions[server_id]):
            return False  # no matching grant
        if remaining:
            policy.permissions[server_id] = remaining
        else:
            del policy.permissions[server_id]  # last grant for the server
        self.save_policy(agent_name, policy)
        return True

    def load_policy(self, agent_name: str) -> Optional[AgentPolicy]:
        if self._agent_id(agent_name) is None:
            return None
        path = self.policy_path_for(agent_name)
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return AgentPolicy(**data)
        except Exception as e:
            logger.error("Could not load the policy for %r: %s", agent_name, e)
            return None
