"""LLM test bot for OpenAI-compatible APIs: OpenAI, Ollama (local or cloud), and others.

Same rules, tools and prompt as the Claude bot (llm_common.py), so the scores are
comparable. It speaks the Chat Completions API through the `openai` package, which
Ollama, LM Studio, vLLM, OpenRouter and similar servers also accept.

    pip install openai          # or: uv sync --extra openai

    # OpenAI
    export OPENAI_API_KEY=sk-...
    python bots/openai_bot.py --provider openai --model gpt-6.1-terra --token fa_...

    # Ollama on this machine (free; no key). Pick a model that supports tools.
    ollama pull qwen3:8b
    OLLAMA_CONTEXT_LENGTH=32768 ollama serve
    python bots/openai_bot.py --provider ollama --model qwen3:8b --token fa_...

    # Ollama cloud
    export OLLAMA_API_KEY=...
    python bots/openai_bot.py --provider ollama-cloud --model gpt-oss:120b --token fa_...

    # Anything else that speaks the OpenAI API
    python bots/openai_bot.py --base-url http://host:port/v1 --api-key-env MY_KEY --model name ...

Memory (--memory):
  compact (default)  The model sees its goal, a STATE sheet the bot keeps from game results
                     (budgets, inventory, entities, rejections, the model's own notes), the
                     latest observe as a compressed MAP, and only the last few turns. Every
                     request is about the same size, so cost grows linearly with turns.
  full               The whole conversation every turn (old results trimmed). Cost grows with
                     turns squared. Kept for A/B comparisons.
Batching (--parallel, default on): the model may send several tool calls per turn; they run in
order and the rest are skipped after a failure, so a failure never cascades into wasted actions.

Small local models make more mistakes than frontier models, so this bot is forgiving:
malformed tool arguments come back to the model as an error (they cost no action),
a tool call written as plain JSON text is still run, and a model that stops calling
tools is nudged a few times before the run is ended.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from types import SimpleNamespace
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))
from arena_sdk import ArenaClient, Run  # noqa: E402
from llm_common import budgets_spent, dumps, finish, run_tool, system_prompt, tool_specs  # noqa: E402
from memory import BATCH_PROMPT, MEMO_PROPERTY, MEMORY_PROMPT, Ledger  # noqa: E402

PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {"base_url": None, "key_env": "OPENAI_API_KEY", "model": "gpt-6.1-terra"},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None, "model": "qwen3:8b"},
    "ollama-cloud": {"base_url": "https://ollama.com/v1", "key_env": "OLLAMA_API_KEY", "model": "gpt-oss:120b"},
}

# USD per 1M tokens: (input, cached input, output). Reasoning tokens are billed as output.
PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-6.1-terra": (1.00, 0.05, 5.00),
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-6-astra": (10.00, 1.00, 50.00),
}


def cost_usd(model: str, prompt: int, cached: int, completion: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return ((prompt - cached) * price[0] + cached * price[1] + completion * price[2]) / 1e6


START = "The run has started. Begin."
NUDGE = "Act only through the tools. Call the next tool now, or call end_run if you are done."
SKIPPED = {"skipped": True, "message": "not run because an earlier call in this turn failed; costs nothing"}
THINK = re.compile(r"<think>.*?</think>", re.S)


def openai_tools(task: dict, strict: bool, memo: bool = False) -> list[dict]:
    tools = []
    for name, desc, schema in tool_specs(task):
        if memo and name != "end_run":
            schema = {**schema, "properties": {**schema["properties"], "memo": MEMO_PROPERTY}}
            if strict:   # strict mode needs every property listed as required
                schema["required"] = [*schema["required"], "memo"]
        fn: dict[str, Any] = {"name": name, "description": desc, "parameters": schema}
        if strict:
            fn["strict"] = True
        tools.append({"type": "function", "function": fn})
    return tools


class ResponsesAdapter:
    """Lets play() use OpenAI's Responses API through the chat.completions.create interface.

    Why: on Chat Completions some models refuse function tools while reasoning is on (gpt-6.1-terra
    then needs reasoning off, gpt-6.1-sol can't call tools at all). The Responses API allows both.
    Earlier tool calls and results are sent back as plain text, so no reasoning items have to be
    passed back and compact memory works unchanged. Requests are not stored (store=False).
    """

    def __init__(self, client, reasoning_effort: str | None = "low"):
        self.client, self.effort = client, reasoning_effort
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    @staticmethod
    def _tools(tools: list[dict]) -> list[dict]:
        out = []
        for t in tools:
            fn = t["function"]
            out.append({"type": "function", "name": fn["name"], "description": fn.get("description", ""),
                        "parameters": fn["parameters"], "strict": bool(fn.get("strict", False))})
        return out

    @staticmethod
    def _input(messages: list[dict]) -> tuple[str, list[dict]]:
        instructions, items, names = [], [], {}
        for m in messages:
            role, content = m["role"], m.get("content") or ""
            if role == "system":
                instructions.append(content)
            elif role == "assistant":
                calls = m.get("tool_calls") or []
                for c in calls:
                    names[c["id"]] = c["function"]["name"]
                text = content
                if calls:
                    text += ("\n" if text else "") + "I called: " + "; ".join(
                        f"{c['function']['name']}({c['function']['arguments']})" for c in calls)
                if text:
                    items.append({"role": "assistant", "content": text})
            elif role == "tool":
                items.append({"role": "user", "content": f"Result of {names.get(m.get('tool_call_id'), 'tool')}: {content}"})
            else:
                items.append({"role": "user", "content": content})
        return "\n\n".join(instructions), items

    def create(self, model: str, messages: list[dict], tools: list[dict] = (), tool_choice: str = "auto", **extra):
        instructions, items = self._input(messages)
        kw: dict[str, Any] = {"model": model, "instructions": instructions, "input": items, "store": False}
        if tools:                             # a plain JSON reply (the floor-plan designer) sends no tools
            kw.update(tools=self._tools(tools), tool_choice=tool_choice)
        effort = extra.pop("reasoning_effort", None) or self.effort
        if effort:
            kw["reasoning"] = {"effort": effort}
        if "parallel_tool_calls" in extra:
            kw["parallel_tool_calls"] = extra.pop("parallel_tool_calls")
        r = self.client.responses.create(**kw)
        calls, texts = [], []
        for item in r.output or []:
            kind = getattr(item, "type", None)
            if kind == "function_call":
                calls.append(SimpleNamespace(id=item.call_id, type="function",
                                             function=SimpleNamespace(name=item.name, arguments=item.arguments)))
            elif kind == "message":
                for part in getattr(item, "content", None) or []:
                    if getattr(part, "type", "") == "output_text":
                        texts.append(part.text)
        u = getattr(r, "usage", None)
        usage = None
        if u is not None:
            cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
            usage = SimpleNamespace(prompt_tokens=u.input_tokens or 0, completion_tokens=u.output_tokens or 0,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=cached))
        msg = SimpleNamespace(content="\n".join(texts) or None, tool_calls=calls or None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls" if calls else "stop")],
                               usage=usage)


def make_client(provider: str, base_url: str | None, api_key: str | None):
    import openai  # imported here so the tests can run without the package

    cfg = PROVIDERS.get(provider, {})
    base_url = base_url or cfg.get("base_url")
    if api_key is None:
        env = cfg.get("key_env")
        api_key = os.environ.get(env, "") if env else ""
        if env and not api_key:
            raise SystemExit(f"set {env} (or pass --api-key-env) for provider {provider!r}")
    # The OpenAI client insists on a key; local Ollama ignores it.
    return openai.OpenAI(base_url=base_url, api_key=api_key or "unused")


@dataclass
class Call:
    id: str | None          # None when the call was parsed out of plain text
    name: str
    raw_args: Any


@dataclass
class Stats:
    memory: str = ""
    turns: int = 0
    tool_calls: int = 0
    bad_calls: int = 0
    skipped_calls: int = 0
    text_calls: int = 0
    nudges: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    largest_prompt: int = 0
    cost_usd: float = 0.0
    stop: str = ""
    errors: list[str] = field(default_factory=list)


def calls_from_text(content: str, allowed: set[str]) -> list[Call]:
    """Recover tool calls a model wrote as JSON text, e.g. {"name": "observe", "arguments": {"radius": 20}}."""
    text = THINK.sub("", content or "")
    dec, out, i = json.JSONDecoder(), [], 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        i = end
        if isinstance(obj, dict) and obj.get("name") in allowed:
            args = obj.get("arguments", obj.get("parameters", {}))
            out.append(Call(None, obj["name"], args))
    return out


def parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if raw in (None, ""):
        return {}
    args = json.loads(raw)
    if not isinstance(args, dict):
        raise ValueError("arguments must be a JSON object")
    return args


def trim_history(messages: list[dict], keep_full: int, limit: int = 240) -> None:
    """Full mode: shorten all but the last `keep_full` tool results (every message is kept, so pairing holds)."""
    results = [m for m in messages if m["role"] == "tool" or m.get("_result")]
    for m in results[:-keep_full] if keep_full else results:
        if len(m["content"]) > limit and not m.get("_trimmed"):
            m["content"] = m["content"][:limit] + "...(trimmed; observe again if you need it)"
            m["_trimmed"] = True


def wire(messages: list[dict]) -> list[dict]:
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def _for_window(name: str, out: dict) -> dict:
    """Compact mode: an observe's payload lives in MAP/STATE, so the turn log keeps only the outcome."""
    if name == "observe" and out.get("ok"):
        return {"ok": True, "data": "shown in MAP and STATE", "budgets": out.get("budgets")}
    return out


def play(run: Run, task: dict, constants: dict, client, model: str, strict: bool = False,
         memory: str = "compact", window: int = 3, parallel: bool = True,
         max_turns: int = 300, max_nudges: int = 3, keep_full: int = 6, reasoning_effort: str | None = None,
         max_cost_usd: float | None = None, log: Callable[..., None] = print,
         transcript: Callable[[dict], None] | None = None, confirm_early_end: float | None = None) -> dict:
    """max_cost_usd: stop before a turn that could push the model bill past this (needs the model in PRICES).
    transcript: called once per turn with what the model wrote and called, for later analysis.
    confirm_early_end: an end_run while more than this share of the ticks is left is answered with a
    reminder; the model has to call end_run again to really end."""
    end_warned = False
    if max_cost_usd is not None and model not in PRICES:
        raise ValueError(f"no price known for {model}; add it to PRICES to use a cost cap")
    compact = memory == "compact"
    tools = openai_tools(task, strict, memo=compact)
    allowed = {t["function"]["name"] for t in tools}
    extra: dict[str, Any] = {} if (parallel and not strict) else {"parallel_tool_calls": False}
    if reasoning_effort:      # reasoning models: thinking tokens are billed as output, the biggest cost left
        extra["reasoning_effort"] = reasoning_effort
    system = system_prompt(task, constants) + (MEMORY_PROMPT if compact else "") + (BATCH_PROMPT if parallel else "")

    ledger = Ledger(task, run.budgets)
    head = [{"role": "system", "content": system}, {"role": "user", "content": START}]
    history: list[dict] = list(head)            # full mode
    turns: list[list[dict]] = []                # compact mode: one list of messages per turn
    stats, idle = Stats(memory=memory), 0

    def request() -> list[dict]:
        if not compact:
            return wire(history)
        msgs = list(head)
        if ledger.map:
            msgs.append({"role": "user", "content": ledger.map})
        for t in turns[-window:] if window else []:
            msgs += t
        msgs.append({"role": "user", "content": ledger.render()})
        return wire(msgs)

    last_turn_cost = 0.0
    try:
        while stats.turns < max_turns:
            if max_cost_usd is not None and stats.cost_usd + 2 * last_turn_cost >= max_cost_usd:
                stats.stop = "cost_cap"
                break
            stats.turns += 1
            try:
                resp = client.chat.completions.create(model=model, messages=request(), tools=tools,
                                                      tool_choice="auto", **extra)
            except Exception as e:  # noqa: BLE001
                # e.g. gpt-6.1-terra: "Function tools with reasoning_effort are not supported ... set
                # reasoning_effort to 'none'". Nothing is billed for the refused call; retry once that way.
                if "reasoning_effort" not in str(e) or extra.get("reasoning_effort") == "none":
                    raise
                log("model refuses tools with reasoning on; retrying with reasoning_effort='none'")
                extra["reasoning_effort"] = "none"
                stats.errors.append("reasoning_effort set to 'none' (the model refused tools with reasoning)")
                resp = client.chat.completions.create(model=model, messages=request(), tools=tools,
                                                      tool_choice="auto", **extra)
            usage = getattr(resp, "usage", None)
            if usage:
                stats.prompt_tokens += usage.prompt_tokens or 0
                stats.completion_tokens += usage.completion_tokens or 0
                stats.largest_prompt = max(stats.largest_prompt, usage.prompt_tokens or 0)
                details = getattr(usage, "prompt_tokens_details", None)
                stats.cached_tokens += (getattr(details, "cached_tokens", 0) or 0) if details else 0
                total = cost_usd(model, stats.prompt_tokens, stats.cached_tokens, stats.completion_tokens)
                if total is not None:
                    last_turn_cost, stats.cost_usd = total - stats.cost_usd, total
            msg = resp.choices[0].message
            content = msg.content or ""
            calls = [Call(tc.id, tc.function.name, tc.function.arguments) for tc in (msg.tool_calls or [])]
            if not parallel:
                calls = calls[:1]

            turn: list[dict] = []
            if calls:
                turn.append({"role": "assistant", "content": content, "tool_calls": [
                    {"id": c.id, "type": "function", "function": {
                        "name": c.name,
                        "arguments": c.raw_args if isinstance(c.raw_args, str) else json.dumps(c.raw_args)}}
                    for c in calls]})
            else:
                calls = calls_from_text(content, allowed)[: None if parallel else 1]
                turn.append({"role": "assistant", "content": content})
                stats.text_calls += len(calls)

            if not calls:
                if resp.choices[0].finish_reason == "content_filter" or idle >= max_nudges:
                    stats.stop = "content_filter" if resp.choices[0].finish_reason == "content_filter" else "no_tool_calls"
                    break
                idle += 1
                stats.nudges += 1
                turn.append({"role": "user", "content": NUDGE})
                (history.extend if not compact else lambda t: turns.append(list(t)))(turn)
                continue
            idle = 0

            ended = failed = False
            for c in calls:
                stats.tool_calls += 1
                args: dict = {}
                if failed or ended:
                    out = SKIPPED
                    stats.skipped_calls += 1
                elif c.name not in allowed:
                    out = {"error": "unknown_tool", "message": f"no tool named {c.name!r}; use one of {sorted(allowed)}"}
                    ledger.record(c.name, args, out)
                else:
                    try:
                        args = parse_args(c.raw_args)
                    except ValueError as e:
                        out = {"error": "bad_arguments", "message": f"arguments are not a JSON object: {e}"}
                        ledger.record(c.name, args, out)
                    else:
                        memo = args.pop("memo", None)
                        if isinstance(memo, str) and memo.strip():
                            ledger.notes = memo.strip()[:600]
                        b = run.budgets
                        total = (task.get("budgets") or {}).get("ticks") or 0
                        early = (c.name == "end_run" and not end_warned and total and confirm_early_end is not None
                                 and (b.get("ticks_left") or 0) > confirm_early_end * total)
                        if early:
                            end_warned = True
                            left = b.get("ticks_left") or 0
                            out = {"ended": False, "message": (
                                f"Not ended yet: you still have {left} of {total} ticks ({left / 3600:.1f} game minutes) "
                                f"and {b.get('actions_left')} actions. Only the window after the build time is scored, so "
                                "ending early gains nothing. Use the time to reach the next tier, or call end_run "
                                "again to really end.")}
                        else:
                            out = run_tool(run, c.name, args)
                        if c.name == "end_run" and not early:
                            ended = True
                        elif c.name != "end_run":
                            ledger.record(c.name, args, out)
                if out is not SKIPPED and ("error" in out or out.get("ok") is False):
                    failed = True
                    stats.bad_calls += "error" in out   # never reached the game: unknown, malformed or refused
                shown = _for_window(c.name, out) if compact else out
                if c.id is not None:
                    turn.append({"role": "tool", "tool_call_id": c.id, "content": dumps(shown)})
                else:
                    turn.append({"role": "user", "_result": True, "content": f"Result of {c.name}: {dumps(shown)}"})

            if compact:
                turns.append(turn)
            else:
                history.extend(turn)
                trim_history(history, keep_full)
            if transcript:
                transcript({"turn": stats.turns, "text": content[:2000], "cost_usd": round(stats.cost_usd, 4),
                            "calls": [{"name": c.name, "args": c.raw_args if isinstance(c.raw_args, str)
                                       else json.dumps(c.raw_args)} for c in calls],
                            "results": [m["content"][:300] for m in turn if m.get("role") == "tool" or m.get("_result")]})
            b = run.budgets
            log(f"turn {stats.turns}: {', '.join(c.name for c in calls)} | actions left {b.get('actions_left')}, "
                f"ticks left {b.get('ticks_left')}, points {ledger.score} | ${stats.cost_usd:.2f}")
            if ended:
                stats.stop = "end_run"
                break
            if budgets_spent(run):
                stats.stop = "budgets_spent"
                break
        else:
            stats.stop = "max_turns"
    except Exception as e:  # model API down, auth error, etc.: still end the run so the slot frees up
        stats.stop = "model_error"
        stats.errors.append(repr(e)[:300])
        log("model call failed:", repr(e)[:300])

    result = finish(run, task)
    stats.cost_usd = round(stats.cost_usd, 4)
    result["bot"] = {"model": model, **vars(stats)}
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get("ARENA_URL", "http://127.0.0.1:8710"))
    ap.add_argument("--token", default=os.environ.get("ARENA_TOKEN", ""))
    ap.add_argument("--task", default="level1-iron")
    ap.add_argument("--mode", default="practice")
    ap.add_argument("--provider", choices=sorted(PROVIDERS), default="ollama")
    ap.add_argument("--model", help="model name; defaults depend on --provider")
    ap.add_argument("--base-url", help="override the provider's API base URL")
    ap.add_argument("--api-key-env", help="name of the env var that holds the API key")
    ap.add_argument("--memory", choices=["compact", "full"], default="compact")
    ap.add_argument("--window", type=int, default=3, help="compact mode: recent turns shown verbatim")
    ap.add_argument("--parallel", action=argparse.BooleanOptionalAction, default=True,
                    help="allow several tool calls per turn (default on)")
    ap.add_argument("--strict", action=argparse.BooleanOptionalAction, default=False,
                    help="OpenAI strict tool schemas (turns batching off)")
    ap.add_argument("--reasoning-effort", choices=["none", "minimal", "low", "medium", "high", "xhigh"],
                    help="for reasoning models; lower means fewer billed thinking tokens")
    ap.add_argument("--max-turns", type=int, default=300)
    ap.add_argument("--max-cost", type=float, help="stop before the model bill for this run passes this many USD")
    ap.add_argument("--api", choices=["chat", "responses"],
                    help="OpenAI API to call (default: responses for --provider openai, chat otherwise)")
    args = ap.parse_args()

    from dotenv_lite import load_env
    load_env()                                   # OPENAI_API_KEY etc. from the project's .env
    api_key = None
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env) or sys.exit(f"{args.api_key_env} is not set")
    client = make_client(args.provider, args.base_url, api_key)
    model = args.model or PROVIDERS[args.provider]["model"]
    effort = args.reasoning_effort
    if (args.api or ("responses" if args.provider == "openai" else "chat")) == "responses":
        client, effort = ResponsesAdapter(client, args.reasoning_effort or "low"), None

    arena = ArenaClient(args.url, args.token)
    rules = arena.rules()
    task = next(t for t in rules["tasks"] if t["id"] == args.task)
    run = arena.start_run(args.task, args.mode)
    print("run", run.id, run.state.get("map"), "model", model, "memory", args.memory)
    result = play(run, task, rules["constants"], client, model, strict=args.strict, memory=args.memory,
                  window=args.window, parallel=args.parallel and not args.strict, max_turns=args.max_turns,
                  reasoning_effort=effort, max_cost_usd=args.max_cost, confirm_early_end=0.25)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
