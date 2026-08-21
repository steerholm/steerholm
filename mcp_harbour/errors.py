from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

# GPARS standard error codes (outside JSON-RPC reserved range)
AUTHORIZATION_DENIED_CODE = -31001
SERVER_UNAVAILABLE_CODE = -31002


def authorization_denied(message: str) -> McpError:
    return McpError(
        ErrorData(
            code=AUTHORIZATION_DENIED_CODE,
            message=message,
            data={"gpars_code": "AUTHORIZATION_DENIED"},
        )
    )


def server_unavailable(server_name: str) -> McpError:
    return McpError(
        ErrorData(
            code=SERVER_UNAVAILABLE_CODE,
            message=f"MCP server '{server_name}' is not reachable.",
            data={"gpars_code": "SERVER_UNAVAILABLE"},
        )
    )
