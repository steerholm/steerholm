from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

# Harbour error codes (outside the JSON-RPC reserved range)
AUTHORIZATION_DENIED_CODE = -31001
SERVER_UNAVAILABLE_CODE = -31002


def authorization_denied(message: str) -> McpError:
    return McpError(
        ErrorData(
            code=AUTHORIZATION_DENIED_CODE,
            message=message,
            data={"error_type": "AUTHORIZATION_DENIED"},
        )
    )


def server_unavailable(server_name: str) -> McpError:
    return McpError(
        ErrorData(
            code=SERVER_UNAVAILABLE_CODE,
            message=f"MCP server '{server_name}' is not reachable.",
            data={"error_type": "SERVER_UNAVAILABLE"},
        )
    )
