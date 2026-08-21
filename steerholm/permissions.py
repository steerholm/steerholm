import re
from fnmatch import fnmatch
from typing import Dict, Any, Optional, List
from .models import AgentPolicy, ToolPermission, ArgumentPolicy
from .errors import authorization_denied


class PermissionEngine:
    def __init__(self, policy: AgentPolicy):
        self.policy = policy

    def check_permission(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any] = None
    ) -> bool:
        """
        Checks if the agent has permission to use the tool on the given server.
        Raises McpError with AUTHORIZATION_DENIED if denied.
        Returns True if allowed.
        """
        if server_name not in self.policy.permissions:
            raise authorization_denied(
                f"Access to server '{server_name}' denied for this agent."
            )

        allowed_tools = self.policy.permissions[server_name]

        matched_permission: Optional[ToolPermission] = None
        for perm in allowed_tools:
            if fnmatch(tool_name, perm.name):
                matched_permission = perm
                break

        if not matched_permission:
            raise authorization_denied(
                f"Tool '{tool_name}' on server '{server_name}' is not allowed."
            )

        if matched_permission.policies:
            self._enforce_policies(matched_permission.policies, arguments or {})

        return True

    def _enforce_policies(
        self, policies: List[ArgumentPolicy], arguments: Dict[str, Any]
    ):
        for policy in policies:
            arg_value = arguments.get(policy.arg_name)

            # An argument policy constrains its argument only when the call actually
            # provides it. A tool that doesn't take the argument is unaffected — so a
            # broad `--tool "*" --args "path=..."` does not reject tools without a
            # path. (You can't reach a resource through an argument you never passed.)
            if arg_value is None:
                continue

            if not self._match_policy(policy, str(arg_value)):
                raise authorization_denied(
                    f"Argument '{policy.arg_name}' value '{arg_value}' does not satisfy policy."
                )

    def _match_policy(self, policy: ArgumentPolicy, value: str) -> bool:
        if policy.match_type == "regex":
            return bool(re.match(policy.pattern, value))
        return fnmatch(value, policy.pattern)
