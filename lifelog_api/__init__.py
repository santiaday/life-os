"""Lifelog iOS app HTTP API.

Mounted under /lifelog/* on the existing MCP FastAPI app
(see mcp_server/server.py::build_app). Per-route bearer auth via
require_token; the MCP path-secret middleware skips this prefix.

Surface (all under /lifelog):
  POST /events/start       create + claim active session
  POST /events/end         close a session
  GET  /events/active      recovery on launch
  GET  /events/recent      history list
  GET  /events/{id}        single event detail
  GET  /events/health      counters for Settings diag screen
  GET  /activity-types     static config (cacheable client-side)
"""
