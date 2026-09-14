from enum import Enum
from typing import List, Dict, Optional
from pydantic import BaseModel, Field


class ServerType(str, Enum):
    stdio = "stdio"
    http = "http"


class Server(BaseModel):
    name: str = Field(..., description="Unique name of the server")
    # Immutable id, minted once at creation. A name can be reused by removing and
    # re-adding a server — pointing it at a different command — so the audit log
    # records the id to tell those apart.
    # Optional: entries written before ids existed load as None.
    id: Optional[str] = Field(default=None, description="Immutable server id (minted at creation)")
    command: str = Field(default="", description="Full command to execute (stdio servers)")
    url: str = Field(default="", description="Server URL (http servers)")
    env: Dict[str, str] = Field(
        default_factory=dict, description="Environment variables"
    )
    server_type: ServerType = Field(
        default=ServerType.stdio, description="Type of MCP server connection"
    )


class ArgumentPolicy(BaseModel):
    """
    Defines a policy for a specific argument of a tool.
    match_type is either "glob" (default) or "regex" (prefix re: in CLI).
    """

    arg_name: str
    match_type: str = Field(default="glob", pattern="^(glob|regex)$")
    pattern: str


class ToolPermission(BaseModel):
    name: str = Field(..., description="Name of the tool (can use glob, e.g. 'read_*')")
    policies: List[ArgumentPolicy] = Field(
        default_factory=list, description="Argument-level restrictions"
    )


class AgentPolicy(BaseModel):
    # Keyed by immutable ids, not names: a name can be reused by removing and
    # re-adding, and a grant that outlived its subject must not attach to
    # whatever takes the name next.
    agent_id: str
    permissions: Dict[str, List[ToolPermission]] = Field(
        ..., description="Map of server id -> list of allowed tools"
    )


class Agent(BaseModel):
    name: str = Field(..., description="Name of the agent")
    # Immutable id, minted once at creation and kept across key rotation. Lets the
    # audit log distinguish a deleted-then-recreated name (same name, new principal).
    # Optional: entries written before ids existed load as None.
    id: Optional[str] = Field(default=None, description="Immutable agent id (minted at creation)")
    key_prefix: str = Field(..., description="First 15 chars of the access key for display")


class AuditSettings(BaseModel):
    """Retention for the decision audit log. Both limits apply — a segment goes
    when it is past the count OR older than the age limit.

    Segment size is deliberately not settable: changing it would leave existing
    segments at the old size (honouring a new one means re-splitting the log), and
    an unwitting large value costs memory and latency on every read.
    """

    max_files: int = Field(
        default=5, description="How many log files to keep (0 = no limit)"
    )
    max_age_days: int = Field(
        default=0, description="Delete segments older than this (0 = no age limit)"
    )


class Config(BaseModel):
    servers: Dict[str, Server] = Field(default_factory=dict)
    agents: Dict[str, Agent] = Field(default_factory=dict)
    # Absent in configs written before retention was configurable; defaults apply.
    audit: AuditSettings = Field(default_factory=AuditSettings)
