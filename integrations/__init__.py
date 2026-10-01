"""
Third-party channel integrations (LinkedIn, Instagram, WhatsApp, ...), each
wrapping a locally-running MCP server. Every integration module exposes a
get_status() function with the same shape so server.py can register uniform
/api/<channel>/status routes without channel-specific branching.
"""
