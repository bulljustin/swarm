"""Per-domain MCP tool handlers (task #518 split from ``mcp/tools.py``).

Each `_*.py` module owns the MCP tool *schemas* AND the handler functions
for a single concern. The aggregator at :mod:`swarm.mcp.tools` imports
their ``TOOLS`` lists and ``HANDLERS`` dicts and merges them into the
unified registry the MCP server publishes.
"""
