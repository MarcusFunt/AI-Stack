from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp"))

with patch.dict(os.environ, {
    "MCP_API_KEY": "test-mcp-api-key",
    "MCP_URL_TOKEN": "test-mcp-url-token",
    "AI_API_KEY": "test-ai-api-key",
}):
    import app as service_app


class InboundMCPToolTests(unittest.TestCase):
    def test_inbound_mcp_exposes_ai_aliases_and_broker_tools(self):
        names = {tool.name for tool in service_app.mcp._tool_manager.list_tools()}

        self.assertTrue({
            "ai.generate",
            "ai.reason",
            "ai.models.list",
            "ai.system.status",
            "mcp_list_tools",
            "mcp_call_tool",
            "ask_local_ai",
        } <= names)


if __name__ == "__main__":
    unittest.main()
