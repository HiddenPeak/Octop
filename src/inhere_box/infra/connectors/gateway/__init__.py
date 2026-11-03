"""In-process MCP gateway for inhere-box-hosted connector adapters."""

from inhere_box.infra.connectors.gateway.langchain import build_gateway_langchain_tools
from inhere_box.infra.connectors.gateway.protocol import handle_mcp_request
from inhere_box.infra.connectors.gateway.registry import mcp_tools_for_kind

__all__ = [
    "build_gateway_langchain_tools",
    "handle_mcp_request",
    "mcp_tools_for_kind",
]
