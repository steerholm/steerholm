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
from .models import Config, Server, Identity, AgentPolicy, ToolPermission, ArgumentPolicy, ServerType

logger = logging.getLogger("mcp_harbour.config")


def _get_config_dir() -> Path:
    override = os.environ.get("MCP_HARBOUR_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "mcp-harbour"
    return Path.home() / ".mcp-harbour"


CONFIG_DIR = _get_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
POLICIES_DIR = CONFIG_DIR / "policies"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4767

# The control token lives under its OWN keyring service, separate from the
# "mcp-harbour" service used for identity keys, so no identity name can ever
# collide with it.
CONTROL_SERVICE = "mcp-harbour-control"
CONTROL_ACCOUNT = "token"


def get_or_create_control_token() -> str:
    """Return the loopback control-plane token, creating it on first use.

    Unlike agent keys (only a hash is stored), this is stored raw so the CLI —
    running as the same user — can read it and present it to the daemon. The
    control channel is loopback-only, so both ends share the user's keyring.
    """
    token = keyring.get_password(CONTROL_SERVICE, CONTROL_ACCOUNT)
    if not token:
        token = "harbour_ctl_" + "".join(
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
            return Config(**data)
        except Exception as e:
            print(f"Warning: Could not load config: {e}")
            return Config()

    def save_config(self):
        with open(CONFIG_FILE, "w") as f:
            f.write(self.config.model_dump_json(indent=2))

    def reload(self):
        self.config = self._load_config()

    # --- Server Management ---
    def add_server(self, name: str, command: str = None, url: str = None) -> Server:
        """Dock a server. Provide command (stdio) or url (http), not both."""
        if name in self.config.servers:
            raise ValueError(f"Server '{name}' already exists.")
        if command and url:
            raise ValueError("Provide command or url, not both.")
        if not command and not url:
            raise ValueError("Provide command (stdio) or url (http).")

        if command:
            server = Server(name=name, command=command, server_type=ServerType.stdio)
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

    # --- Identity Management ---
    def add_identity(self, name: str) -> str:
        """Create an identity, generate an API key, hash it, store in keyring.
        Returns the api_key. Only available at creation time."""
        if name in self.config.identities:
            raise ValueError(f"Identity '{name}' already exists.")
        alphabet = string.ascii_letters + string.digits
        token = "".join(secrets.choice(alphabet) for _ in range(32))
        api_key = f"harbour_sk_{token}"
        key_prefix = api_key[:15] + "..."

        hashed = bcrypt.hashpw(api_key.encode(), bcrypt.gensalt())
        keyring.set_password("mcp-harbour", name, hashed.decode())

        self.config.identities[name] = Identity(name=name, key_prefix=key_prefix)
        self.save_config()
        return api_key

    def get_identity(self, name: str) -> Optional[Identity]:
        return self.config.identities.get(name)

    def remove_identity(self, name: str):
        """Remove an identity, its keyring entry, and its policy."""
        if name not in self.config.identities:
            raise ValueError(f"Identity '{name}' not found.")
        try:
            keyring.delete_password("mcp-harbour", name)
        except keyring.errors.PasswordDeleteError:
            pass  # entry already absent — nothing to remove
        except Exception as e:
            # Don't fail the removal, but surface it: a lingering keyring entry
            # must not be able to authenticate a removed identity.
            logger.warning("Could not delete keyring entry for '%s': %s", name, e)
        if name in self.config.identities:
            del self.config.identities[name]
            self.save_config()
            policy_path = self._get_policy_path(name)
            if policy_path.exists():
                try:
                    policy_path.unlink()
                except OSError:
                    pass

    def list_identities(self) -> list:
        return list(self.config.identities.values())

    # --- Policy Management ---
    def _get_policy_path(self, identity_name: str) -> Path:
        return POLICIES_DIR / f"{identity_name}.json"

    def create_policy(self, identity_name: str) -> AgentPolicy:
        policy = AgentPolicy(identity_name=identity_name, permissions={})
        self.save_policy(policy)
        return policy

    def save_policy(self, policy: AgentPolicy):
        path = self._get_policy_path(policy.identity_name)
        with open(path, "w") as f:
            f.write(policy.model_dump_json(indent=2))

    def grant_permission(self, identity_name: str, server_name: str,
                         tool: str = "*", arg_policies: List[str] = None):
        """Grant a tool permission to an identity on a server.
        arg_policies: list of 'arg=pattern' or 'arg=re:pattern' strings."""
        if identity_name not in self.config.identities:
            raise ValueError(f"Identity '{identity_name}' not found.")
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

        policy = self.load_policy(identity_name)
        if not policy:
            policy = self.create_policy(identity_name)

        if server_name not in policy.permissions:
            policy.permissions[server_name] = []

        policy.permissions[server_name].append(ToolPermission(name=tool, policies=policies))
        self.save_policy(policy)

    def load_policy(self, identity_name: str) -> Optional[AgentPolicy]:
        path = self._get_policy_path(identity_name)
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return AgentPolicy(**data)
        except Exception as e:
            print(f"Error loading policy for {identity_name}: {e}")
            return None
