#!/usr/bin/env python3
"""Entry point — adds src/ to path then starts the MCP server."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from screener_mcp.server import main

if __name__ == "__main__":
    main()
