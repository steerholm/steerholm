from enum import Enum
from typing import List, Dict
from pydantic import BaseModel, Field


class ServerType(str, Enum):
    stdio = "stdio"
    http = "http"


class Server(BaseModel):
    name: str = Field(..., description="Unique name of the docked ship")
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
    identity_name: str
    permissions: Dict[str, List[ToolPermission]] = Field(
        ..., description="Map of server_name -> list of allowed tools"
    )


class Identity(BaseModel):
    name: str = Field(..., description="Name of the identity (Captain)")
    key_prefix: str = Field(..., description="First 15 chars of API key for display")


class Config(BaseModel):
    servers: Dict[str, Server] = Field(default_factory=dict)
    identities: Dict[str, Identity] = Field(default_factory=dict)
