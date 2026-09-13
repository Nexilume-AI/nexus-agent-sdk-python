"""A FastMCP tool exposed through one explicit Nexus capability mapping."""

import os

from fastmcp import Context, FastMCP

from nexus_agent import CapabilityRegistration, NexusAgentClient, NexusAgentServer
from nexus_agent.fastmcp import FastMCPBridge


mcp = FastMCP("Nexus Verilog Agent")


@mcp.tool
async def lint_verilog(source: str, ctx: Context) -> dict:
    """Lint one Verilog source string."""

    await ctx.report_progress(1, 2, "parsing")
    result = {
        "ok": "endmodule" in source,
        "diagnostics": [] if "endmodule" in source else ["missing endmodule"],
    }
    await ctx.report_progress(2, 2, "complete")
    return result


server = NexusAgentServer(
    "0.0.0.0", 9443,
    cert_file="agent.crt", key_file="agent.key",
)
router = NexusAgentClient(
    "https://192.168.1.1:7443",
    token=os.environ.get("NEXUS_AGENT_TOKEN"),
)

bridge = FastMCPBridge(
    mcp,
    server,
    {
        "lint_verilog": CapabilityRegistration(
            intent="chip.verilog.verify.lint",
            origin="agent://demo/fastmcp-linter",
            endpoint="https://agent.example.test:9443/invoke",
            tenant="demo",
        ),
    },
)

# Enumerates FastMCP tools, validates the mapping, attaches async invocation,
# registers/renews the AFIB route, and converts FastMCP results to Nexus JSON.
bridge.serve_registered(router)
