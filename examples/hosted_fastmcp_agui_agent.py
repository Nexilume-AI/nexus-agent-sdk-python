"""FastMCP Agent with caller-delegated Workspace and private feedback."""

from nexus_agent.fastmcp import CurrentNexusMCP, NexusMCPServer


server = NexusMCPServer("Nexus hosted research Agent")
server.enable_workspace_tools({
    "connection.list",
    "connection.create",
    "connection.test",
    "connection.bind",
    "files.read",
    "files.write",
    "command.execute",
})


@server.tool
async def research(
    topic: str,
    nexus=CurrentNexusMCP(),
) -> dict[str, str]:
    await nexus.feedback.progress(0, 2, "Reading workspace")
    source = await nexus.workspace.read_text("input.md")
    with nexus.trace.step("research-topic"):
        result = await nexus.terminal.run("python analyze.py", cwd=".", timeout=120)
        nexus.memory.add(
            f"The Agent researched {topic}.",
            kind="workflow",
            consent="approved",
            license="internal",
        )
        nexus.output.created(
            "outputs/research.md",
            content_type="text/markdown",
            producer_step="research-topic",
            license="internal",
        )
    await nexus.feedback.progress(2, 2, "Completed")
    return {
        "status": "completed",
        "source": source,
        "stdout": result.stdout,
        "output": "outputs/research.md",
    }


if __name__ == "__main__":
    server.run(transport="streamable-http", host="0.0.0.0", port=8000)
