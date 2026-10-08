"""GigaCode CLI runtime (``api_mode: gigacode_cli``).

Hermes keeps sessions, memory, cron and access control; the whole user turn runs inside a
``gigacode`` CLI process that calls Hermes tools back through a per-run loopback MCP bridge.
Entry point: ``agent/gigacode_runtime.py::run_gigacode_turn``; process transport:
``agent/transports/gigacode_cli_session.py``; bridge: ``agent/transports/gigacode_tools_mcp_server.py``.
Operator guide: ``website/docs/user-guide/features/gigacode-runtime.md``.
"""
