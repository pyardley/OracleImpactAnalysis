"""Claude tool-use agent loop (PROMPT.md 5.5), built on the Python SDK's beta
Tool Runner (`client.beta.messages.tool_runner`) so tool dispatch/looping isn't
hand-rolled - our own code only supplies the tools (oia.agent.tools), the
system prompt (oia.agent.grounding), and progress reporting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anthropic
import networkx as nx

from oia.agent.grounding import SYSTEM_PROMPT
from oia.agent.tools import build_tools
from oia.config.settings import Settings
from oia.graph.sources import load_object_sources
from oia.storage.sqlite_store import SqliteStore

if TYPE_CHECKING:
    from rich.console import Console

MAX_ITERATIONS = 15


def _build_runner(client: anthropic.Anthropic, settings: Settings, question: str, tools: list, include_effort: bool):
    kwargs: dict = {
        "model": settings.agent.model,
        "max_tokens": settings.agent.max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": question}],
        "tools": tools,
        "max_iterations": MAX_ITERATIONS,
    }
    if include_effort:
        # Adaptive-effort control isn't supported on every model (e.g. Haiku
        # rejects it outright) - run_agent retries without it on that specific error.
        kwargs["output_config"] = {"effort": settings.agent.effort}
    return client.beta.messages.tool_runner(**kwargs)


def _drain_with_progress(runner, console: "Console | None", stream: bool) -> None:
    for message in runner:
        if console and stream:
            for block in message.content:
                if block.type == "tool_use":
                    console.print(f"[dim]  calling {block.name}({block.input})[/dim]")


def run_agent(
    settings: Settings,
    g: nx.MultiDiGraph,
    question: str,
    console: Console | None = None,
    stream: bool = True,
) -> str:
    store = SqliteStore(settings.sqlite_path)
    try:
        unresolved = [dict(r) for r in store.unresolved_lineage()]
        sources = load_object_sources(store)
    finally:
        store.close()

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    tools = build_tools(g, unresolved, sources)

    runner = _build_runner(client, settings, question, tools, include_effort=True)
    try:
        _drain_with_progress(runner, console, stream)
    except anthropic.BadRequestError as exc:
        if "effort" not in str(exc).lower():
            raise
        runner = _build_runner(client, settings, question, tools, include_effort=False)
        _drain_with_progress(runner, console, stream)

    final = runner.until_done()
    text = "".join(block.text for block in final.content if block.type == "text")

    if console and stream:
        console.print(f"\n{text}")

    return text
