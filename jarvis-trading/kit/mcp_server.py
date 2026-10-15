"""mcp_server.py - a tiny MCP server that gives an AI app (Claude Desktop, Claude Code, a voice agent)
READ-ONLY views of your Jarvus fleet, plus the two risk-reducing actions HALT and PAUSE.
There are NO trading tools. Run it on the machine that holds the report files, never on the one with keys.

  pip install mcp
  set JARVUS_DIR=C:\\jarvus_state   (folder holding HALT, paused\\, audit.jsonl)
  set JARVUS_REPORT=C:\\jarvus_state\\report.json   (written by jarvus_reporter.py --json)
  set JARVUS_FLEET=C:\\jarvus_state\\fleet.json     (list of bot dicts, see the 'Fleet feed' sheet)
  python mcp_server.py
"""
import os
import policy, tools

try:                                   # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                    # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

CFG = policy.Config(state_dir=os.environ.get("JARVUS_DIR", "."))
REPORT = os.environ.get("JARVUS_REPORT", os.path.join(CFG.state_dir, "report.json"))
FLEET = os.environ.get("JARVUS_FLEET", os.path.join(CFG.state_dir, "fleet.json"))

mcp = _Server("jarvus-readonly", instructions=(
    "Read-only view of a PAPER-trading fleet plus HALT and PAUSE. There are no trading tools. "
    "Quote only numbers returned by these tools. If a tool fails, say so; never guess numbers."))

@mcp.tool()
def get_status() -> dict:
    """Paper-trading status: today's paper P&L, trades, paused bots, HALT flag, and how old the report is."""
    return tools.fleet_status(CFG, REPORT)

@mcp.tool()
def get_daily_report() -> str:
    """The daily report as short bullet lines."""
    return tools.daily_report_text(REPORT)

@mcp.tool()
def get_bot(bot_id: str) -> dict:
    """Details for one bot, for example A073."""
    return tools.get_bot(FLEET, bot_id)

@mcp.tool()
def halt_all(reason: str = "requested by tool") -> dict:
    """EMERGENCY STOP: creates the HALT flag. Nothing restarts until Abhi deletes it at the keyboard."""
    return tools.halt_all(CFG, reason)

@mcp.tool()
def pause_bot(bot_id: str) -> dict:
    """Pause one bot (for example A073). Cannot be undone by this server."""
    return tools.pause_one(CFG, bot_id)

if __name__ == "__main__":
    mcp.run()
