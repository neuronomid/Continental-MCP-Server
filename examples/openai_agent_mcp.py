#!/usr/bin/env python3
"""Minimal OpenAI-compatible agent that consumes the pm-research-engine MCP server.

The MCP server (`python -m pmre serve-mcp`) speaks **Streamable HTTP** with a
bearer token — the same transport the OpenAI Agents SDK and OpenAI's hosted
`{"type": "mcp"}` Responses tool use. So any OpenAI-compatible agent can call
these 16 read-only research tools directly.

Run it:
    pip install openai-agents            # the `openai-agents` package
    export OPENAI_API_KEY=sk-...
    export PMRE_MCP_URL=http://127.0.0.1:8090/mcp
    export PMRE_MCP_BEARER_TOKEN=dev-mcp-token
    python examples/openai_agent_mcp.py

The agent auto-discovers every tool via tools/list, so you don't hand-wire them.
Everything it returns is evidence (n, confidence intervals, freshness), not advice.
"""
import asyncio
import os

from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp

MCP_URL = os.environ.get("PMRE_MCP_URL", "http://127.0.0.1:8090/mcp")
MCP_TOKEN = os.environ.get("PMRE_MCP_BEARER_TOKEN", "dev-mcp-token")

QUESTION = (
    "Is the engine healthy right now, and what is the single strongest strategy "
    "candidate by CI-lower net EV? Quote its n, win rate, and 95% CI-lower, and "
    "remind me these are evidence, not advice."
)


async def main() -> None:
    # One line wires the whole tool catalogue in over Streamable HTTP + bearer auth.
    async with MCPServerStreamableHttp(
        name="pm-research-engine",
        params={"url": MCP_URL, "headers": {"Authorization": f"Bearer {MCP_TOKEN}"}},
        cache_tools_list=True,
    ) as server:
        agent = Agent(
            name="pm-research-analyst",
            model="gpt-4o-mini",  # any OpenAI (or OpenAI-compatible) model
            instructions=(
                "You are a research analyst for Polymarket BTC 5-minute markets. "
                "Use the MCP tools to fetch evidence. Never give trading advice; "
                "decisions are made on CI lower bounds, never point estimates."
            ),
            mcp_servers=[server],
        )
        result = await Runner.run(agent, QUESTION)
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
