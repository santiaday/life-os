"""Pydantic models for typed reads from fact_*/mart_* tables.

Populated as each phase lands. Models are read-only DTOs — the canonical
schema lives in db/migrations/, and these mirror it for ergonomic access from
Python tools (notably the MCP server).
"""
