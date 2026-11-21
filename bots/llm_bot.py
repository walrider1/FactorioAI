"""LLM baseline: Claude plays through the public game API with one tool per verb.

    pip install anthropic
    export ANTHROPIC_API_KEY=...           # or `ant auth login`
    python bots/llm_bot.py --url http://127.0.0.1:8710 --token fa_... --task level1-iron

This is the organizer's reference "AI" baseline (it replaces the pilot's Gemini
test bot). It is deliberately plain: no planning code, just the rules and tools.
A team's job is to beat it. For OpenAI or Ollama models, see openai_bot.py; both
bots share tools and prompt via llm_common.py, so their scores are comparable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import anthropic

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))
from arena_sdk import ArenaClient, Run  # noqa: E402
from llm_common import budgets_spent, finish, run_tool, system_prompt, tool_specs  # noqa: E402

MODEL = "claude-opus-5-5"


def anthropic_tools(task: dict) -> list[dict]:
    return [{"name": n, "description": d, "strict": True, "input_schema": s} for n, d, s in tool_specs(task)]


def play(run: Run, task: dict, constants: dict, max_turns: int = 400) -> dict:
    client = anthropic.Anthropic()
    tools = anthropic_tools(task)
    messages: list = [{"role": "user", "content": "The run has started. Begin."}]
    for _ in range(max_turns):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high"},
            cache_control={"type": "ephemeral"},
            system=system_prompt(task, constants),
            tools=tools,
            messages=messages,
        )
        if response.stop_reason == "refusal":
            print("model declined:", response.stop_details)
            break
        messages.append({"role": "assistant", "content": response.content})
        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            break
        results, ended = [], False
        for call in calls:
            out = run_tool(run, call.name, dict(call.input))
            ended = ended or call.name == "end_run"
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(out)})
        messages.append({"role": "user", "content": results})
        if ended or budgets_spent(run):
            break
    return finish(run, task)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ARENA_URL", "http://127.0.0.1:8710"))
    ap.add_argument("--token", default=os.environ.get("ARENA_TOKEN", ""))
    ap.add_argument("--task", default="level1-iron")
    ap.add_argument("--mode", default="practice")
    args = ap.parse_args()
    arena = ArenaClient(args.url, args.token)
    rules = arena.rules()
    task = next(t for t in rules["tasks"] if t["id"] == args.task)
    run = arena.start_run(args.task, args.mode)
    print("run", run.id, run.state.get("map"))
    print(json.dumps(play(run, task, rules["constants"]), indent=2))


if __name__ == "__main__":
    main()
