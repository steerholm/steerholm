import os
import sys
import json
import logging
import secrets
import string
from pathlib import Path
from typing import Optional, List
import bcrypt
import keyring
from .models import Config, Server, Agent, AgentPolicy, ToolPermission, ArgumentPolicy, ServerType

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

# The control token lives under its OWN keyring service, separate from the
# "steerholm" service used for agent access keys, so no agent name can ever
# collide with it.
CONTROL_SERVICE = "steerholm-control"
CONTROL_ACCOUNT = "token"


def get_or_create_control_token() -> str:
    """Return the loopback control-plane token, creating it on first use.

    Unlike agent keys (only a hash is stored), this is stored raw so the CLI —
    running as the same user — can read it and present it to the daemon. The
    control channel is loopback-only, so both ends share the user's keyring.
    """
    token = keyring.get_password(CONTROL_SERVICE, CONTROL_ACCOUNT)
    if not token:
        token = "steer_ctl_" + "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(32)
        )
        keyring.set_password(CONTROL_SERVICE, CONTROL_ACCOUNT, token)
    return token


class ConfigManager:
    def __init__(self):
        self._ensure_dirs()
        self.config = self._load_config()

    def _ensure_dirs(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        POLICIES_DIR.mkdir(parents=True, exist_ok=True)

    def _load_config(self) -> Config:
        if not CONFIG_FILE.exists():
            return Config()
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            data = self._migrate_config(data)
            return Config(**data)
        except Exception as e:
            print(f"Warning: Could not load config: {e}")
            return Config()

    @staticmethod
    def _migrate_config(data: dict) -> dict:
        """Transparently upgrade a legacy config to the current schema. A config
        written before the agent rename stored agents under an `identities` key;
        map it across so old installs load unchanged (and get rewritten to the new
        schema on the next save)."""
        if isinstance(data, dict) and "identities" in data and "agents" not in data:
            data = dict(data)  # copy first: don't mutate the caller's dict
            data["agents"] = data.pop("identities")
        return data

    def save_config(self):
        with open(CONFIG_FILE, "w") as f:
            f.write(self.config.model_dump_json(indent=2))

    def reload(self):
        self.config = self._load_config()

    # --- Server Management ---
    def add_server(self, name: str, command: str = None, url: str = None,
                   env: dict = None) -> Server:
        """Dock a server. Provide command (stdio) or url (http), not both."""
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

        if command:
            server = Server(name=name, command=command, env=env or {},
                            server_type=ServerType.stdio)
        else:
            server = Server(name=name, url=url, server_type=ServerType.http)

        self.config.servers[name] = server
        self.save_config()
        return server

    def remove_server(self, name: str):
        if name not in self.config.servers:
            raise ValueError(f"Server '{name}' not found.")
        del self.config.servers[name]
        self.save_config()

    def get_server(self, name: str) -> Optional[Server]:
        return self.config.servers.get(name)

    def list_servers(self) -> List[Server]:
        return list(self.config.servers.values())

    # --- Agent Management ---
    def _generate_access_key(self, name: str) -> str:
        """Mint a fresh access key, hash it, and store the hash in the keyring.
        Returns the raw key — only available at creation/rotation time."""
        alphabet = string.ascii_letters + string.digits
        token = "".join(secrets.choice(alphabet) for _ in range(32))
        access_key = f"steer_sk_{token}"
        hashed = bcrypt.hashpw(access_key.encode(), bcrypt.gensalt())
        keyring.set_password("steerholm", name, hashed.decode())
        return access_key

    def add_agent(self, name: str) -> str:
        """Create an agent, generate an access key, hash it, store in keyring.
        Returns the access key. Only available at creation time."""
        if name in self.config.agents:
            raise ValueError(f"Agent '{name}' already exists.")
        access_key = self._generate_access_key(name)
        self.config.agents[name] = Agent(name=name, key_prefix=access_key[:15] + "...")
        self.save_config()
        return access_key

    def rotate_agent_key(self, name: str) -> str:
        """Generate a new access key for an existing agent, keeping its grants.
        Returns the new key; the old one stops working immediately."""
        if name not in self.config.agents:
            raise ValueError(f"Agent '{name}' not found.")
        access_key = self._generate_access_key(name)
        self.config.agents[name] = Agent(name=name, key_prefix=access_key[:15] + "...")
        self.save_config()
        return access_key

    def get_agent(self, name: str) -> Optional[Agent]:
        return self.config.agents.get(name)

    def remove_agent(self, name: str):
        """Remove an agent, its keyring entry, and its policy."""
        if name not in self.config.agents:
            raise ValueError(f"Agent '{name}' not found.")
        try:
            keyring.delete_password("steerholm", name)
        except keyring.errors.PasswordDeleteError:
            pass  # entry already absent — nothing to remove
        except Exception as e:
            # Don't fail the removal, but surface it: a lingering keyring entry
            # must not be able to authenticate a removed agent.
            logger.warning("Could not delete keyring entry for '%s': %s", name, e)
        if name in self.config.agents:
            del self.config.agents[name]
            self.save_config()
            policy_path = self._get_policy_path(name)
            if policy_path.exists():
                try:
                    policy_path.unlink()
                except OSError:
                    pass

    def list_agents(self) -> list:
        return list(self.config.agents.values())

    # --- Policy Management ---
    def _get_policy_path(self, agent_name: str) -> Path:
        return POLICIES_DIR / f"{agent_name}.json"

    def create_policy(self, agent_name: str) -> AgentPolicy:
        policy = AgentPolicy(agent_name=agent_name, permissions={})
        self.save_policy(policy)
        return policy

    def save_policy(self, policy: AgentPolicy):
        path = self._get_policy_path(policy.agent_name)
        with open(path, "w") as f:
            f.write(policy.model_dump_json(indent=2))

    def grant_permission(self, agent_name: str, server_name: str,
                         tool: str = "*", arg_policies: List[str] = None):
        """Grant a tool permission to an agent on a server.
        arg_policies: list of 'arg=pattern' or 'arg=re:pattern' strings."""
        if agent_name not in self.config.agents:
            raise ValueError(f"Agent '{agent_name}' not found.")
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

        if server_name not in policy.permissions:
            policy.permissions[server_name] = []

        policy.permissions[server_name].append(ToolPermission(name=tool, policies=policies))
        self.save_policy(policy)

    def revoke_permission(self, agent_name: str, server_name: str,
                          tool: str = None) -> bool:
        """Remove access an agent has to a server. With `tool`, drop only grants
        whose tool pattern matches it exactly; without, drop the whole server.
        Returns True if anything was removed."""
        if agent_name not in self.config.agents:
            raise ValueError(f"Agent '{agent_name}' not found.")
        policy = self.load_policy(agent_name)
        if not policy or server_name not in policy.permissions:
            return False

        if tool is None:
            del policy.permissions[server_name]
            self.save_policy(policy)
            return True

        remaining = [p for p in policy.permissions[server_name] if p.name != tool]
        if len(remaining) == len(policy.permissions[server_name]):
            return False  # no matching grant
        if remaining:
            policy.permissions[server_name] = remaining
        else:
            del policy.permissions[server_name]  # last grant for the server
        self.save_policy(policy)
        return True

    def load_policy(self, agent_name: str) -> Optional[AgentPolicy]:
        path = self._get_policy_path(agent_name)
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and "identity_name" in data and "agent_name" not in data:
                data["agent_name"] = data.pop("identity_name")  # legacy policy file
            return AgentPolicy(**data)
        except Exception as e:
            print(f"Error loading policy for {agent_name}: {e}")
            return None
