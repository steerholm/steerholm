"""Tests for GPARS error code factories."""

from mcp.shared.exceptions import McpError
from mcp_harbour.errors import (
    authorization_denied,
    server_unavailable,
    AUTHORIZATION_DENIED_CODE,
    SERVER_UNAVAILABLE_CODE,
)


class TestAuthorizationDenied:
    def test_returns_mcp_error(self):
        err = authorization_denied("test message")
        assert isinstance(err, McpError)

    def test_has_correct_code(self):
        err = authorization_denied("test")
        assert err.error.code == AUTHORIZATION_DENIED_CODE

    def test_has_gpars_data(self):
        err = authorization_denied("test")
        assert err.error.data == {"gpars_code": "AUTHORIZATION_DENIED"}

    def test_preserves_message(self):
        err = authorization_denied("Access to /etc denied")
        assert err.error.message == "Access to /etc denied"


class TestServerUnavailable:
    def test_returns_mcp_error(self):
        err = server_unavailable("filesystem")
        assert isinstance(err, McpError)

    def test_has_correct_code(self):
        err = server_unavailable("filesystem")
        assert err.error.code == SERVER_UNAVAILABLE_CODE

    def test_has_gpars_data(self):
        err = server_unavailable("filesystem")
        assert err.error.data == {"gpars_code": "SERVER_UNAVAILABLE"}

    def test_includes_server_name_in_message(self):
        err = server_unavailable("my-database")
        assert "my-database" in err.error.message
