"""The OpenAI/Ollama bot's loop, driven by a fake model: no API key, no network, no cost."""

import json

import pytest
from types import SimpleNamespace as NS

import openai_bot
from arena_sdk import ArenaClient


def tool_call(i, name, args):
    return NS(id=f"call_{i}", type="function",
              function=NS(name=name, arguments=args if isinstance(args, str) else json.dumps(args)))


def reply(content=None, calls=None, finish="stop"):
    msg = NS(content=content, tool_calls=calls)
    return NS(choices=[NS(message=msg, finish_reason=finish)],
              usage=NS(prompt_tokens=100, completion_tokens=10))


class FakeModel:
    """Plays a fixed script and records every request, like client.chat.completions.create."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw)))   # must be JSON-serialisable, as on the wire
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def level1(http, token):
    client = ArenaClient(token=token, http=http)
    rules = client.rules()
    task = next(t for t in rules["tasks"] if t["id"] == "level1-iron")
    return client.start_run("level1-iron"), task, rules["constants"]


@pytest.mark.parametrize("memory", ["full", "compact"])
def test_messy_model_is_handled_and_run_scored(http, token, memory):
    run, task, constants = level1(http, token)
    model = FakeModel([
        reply(calls=[tool_call(1, "observe", {"radius": 20})]),
        reply(calls=[tool_call(2, "place", "{not json")]),                    # malformed arguments
        reply(calls=[tool_call(3, "teleport", {"x": 0, "y": 0})]),            # unknown tool
        reply(calls=[tool_call(4, "place", {"entity": "iron-chest", "x": 0, "y": 0, "direction": "north"})]),  # L1 bans it
        reply(content='<think>hmm</think> Let me wait. {"name": "advance", "arguments": {"ticks": 60}}'),
        reply(content="I think the factory is fine."),                          # no tool call -> nudge
        reply(calls=[tool_call(5, "end_run", {})]),
    ])
    result = openai_bot.play(run, task, constants, model, "fake-model", memory=memory, window=20,
                             log=lambda *a: None)

    assert result["status"] == "finished"
    bot = result["bot"]
    assert bot["stop"] == "end_run"
    assert bot["bad_calls"] == 3 and bot["text_calls"] == 1 and bot["nudges"] == 1
    assert bot["prompt_tokens"] == 700
    # malformed / unknown / disallowed calls cost nothing; the text call advanced 60 ticks; finish spent the rest
    assert result["budgets_used"]["actions"] == 0
    assert result["budgets_used"]["observes"] == 1
    assert result["budgets_used"]["ticks"] == 36_000

    last = model.requests[-1]["messages"]
    # every assistant tool call is answered by exactly one tool message, in order
    for i, m in enumerate(last):
        for tc in m.get("tool_calls") or []:
            assert last[i + 1]["role"] == "tool" and last[i + 1]["tool_call_id"] == tc["id"]
    tool_out = {m["tool_call_id"]: json.loads(m["content"]) for m in last if m["role"] == "tool"}
    if memory == "compact":
        assert last[-1]["content"].startswith("STATE") and "entity_not_allowed" in last[-1]["content"]
        assert any(m["content"].startswith("MAP from your latest observe") for m in last)
        assert tool_out["call_1"]["data"] == "shown in MAP and STATE"
    assert tool_out["call_2"]["error"] == "bad_arguments"
    assert tool_out["call_3"]["error"] == "unknown_tool"
    assert tool_out["call_4"]["error"] == "entity_not_allowed"
    assert any(m["role"] == "user" and m["content"].startswith("Result of advance:") for m in last)
    assert any(m["content"] == openai_bot.NUDGE for m in last)
    # tools offered match the task's verbs; no private keys leak onto the wire
    names = {t["function"]["name"] for t in model.requests[0]["tools"]}
    assert names == set(task["verbs"]) | {"end_run"}
    assert not any(k.startswith("_") for m in last for k in m)


def test_silent_model_stops_after_nudges(http, token):
    run, task, constants = level1(http, token)
    model = FakeModel([reply(content="...")] * 4)
    result = openai_bot.play(run, task, constants, model, "fake", max_nudges=3, log=lambda *a: None)
    assert result["status"] == "finished"
    assert result["bot"]["stop"] == "no_tool_calls" and result["bot"]["nudges"] == 3


def test_model_outage_still_ends_the_run(http, token):
    run, task, constants = level1(http, token)
    model = FakeModel([reply(calls=[tool_call(1, "observe", {"radius": 5})]), ConnectionError("ollama down")])
    result = openai_bot.play(run, task, constants, model, "fake", log=lambda *a: None)
    assert result["status"] == "finished"
    assert result["bot"]["stop"] == "model_error"
    assert "ollama down" in result["bot"]["errors"][0]


def test_strict_tools_follow_openai_rules(settings):
    task = settings.tasks["level3-delivery"].model_dump()
    tools = openai_bot.openai_tools(task, strict=True)
    for t in tools:
        fn = t["function"]
        assert fn["strict"] is True
        assert fn["parameters"]["additionalProperties"] is False
        assert set(fn["parameters"]["required"]) == set(fn["parameters"]["properties"])
    assert "strict" not in openai_bot.openai_tools(task, strict=False)[0]["function"]


def test_trim_history_keeps_recent_results():
    msgs = [{"role": "tool", "tool_call_id": str(i), "content": "x" * 1000} for i in range(10)]
    openai_bot.trim_history(msgs, keep_full=3)
    assert all(len(m["content"]) < 400 for m in msgs[:7])
    assert all(len(m["content"]) == 1000 for m in msgs[7:])


def test_real_openai_sdk_against_mock_server(http, token):
    """The actual `openai` package parses an Ollama-style reply and our requests pass its checks."""
    openai = pytest.importorskip("openai")
    import httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "call_a", "type": "function",
                   "function": {"name": "observe", "arguments": "{\"radius\": 10}"}}]}
            finish = "tool_calls"
        else:
            msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "call_b", "type": "function",
                   "function": {"name": "end_run", "arguments": "{}"}}]}
            finish = "tool_calls"
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                                         "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}})

    client = openai.OpenAI(base_url="http://localhost:11434/v1", api_key="unused",
                           http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    run, task, constants = level1(http, token)
    result = openai_bot.play(run, task, constants, client, "qwen3:8b", memory="full", log=lambda *a: None)
    assert result["bot"]["stop"] == "end_run" and result["budgets_used"]["observes"] == 1
    assert seen[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_a",
                                       "content": seen[1]["messages"][-1]["content"]}
    assert seen[0]["tool_choice"] == "auto" and "parallel_tool_calls" not in seen[0]


# ---------------------------------------------------------------- compact memory and batching

def same_map_runs(http, token, manager, n, seed=101):
    """n practice runs on the same seed (organizer-only seed override), wrapped in SDK Run objects."""
    from arena_sdk import Run
    client = ArenaClient(token=token, http=http)
    rules = client.rules()
    task = next(t for t in rules["tasks"] if t["id"] == "level1-iron")
    for _ in range(n):
        s = manager.create_run("alpha", "level1-iron", "practice", seed_override=seed)
        run = Run(client, s.id, {})
        run.wait_until_active()
        yield run, task, rules["constants"]


class ScriptReplay(FakeModel):
    """Replays a recorded action list, one call per turn (memo attached), and measures prompt size."""

    def __init__(self, actions):
        super().__init__([])
        self.actions, self.sizes, self.i = list(actions), [], 0

    def create(self, **kw):
        self.requests.append(kw)
        self.sizes.append(len(json.dumps(kw["messages"])))
        self.i += 1
        if self.actions:
            a = dict(self.actions.pop(0))
            name = a.pop("type")
            a["memo"] = f"step {self.i}: building the iron line"
        else:
            name, a = "end_run", {}
        return reply(calls=[tool_call(self.i, name, a)], finish="tool_calls")


def test_compact_memory_keeps_prompts_flat_and_scores_the_same(http, token, manager):
    import scripted_bot
    from arena_sdk import Run

    runs = same_map_runs(http, token, manager, 2)
    run, task, constants = next(runs)
    recorded, orig = [], Run.act

    def rec(self, action):
        recorded.append(action)
        return orig(self, action)

    Run.act = rec
    try:
        baseline = scripted_bot.play(run, log=lambda *a: None)
    finally:
        Run.act = orig

    run2, task, constants = next(runs)
    model = ScriptReplay(recorded)
    result = openai_bot.play(run2, task, constants, model, "fake", memory="compact", log=lambda *a: None)
    assert result["score"] == baseline["score"] > 900
    sizes = model.sizes
    assert max(sizes[10:]) < 1.3 * min(sizes[10:]), sizes          # flat, not growing with turns
    last = model.requests[-1]["messages"]
    state = last[-1]["content"]
    assert "Your notes: step" in state and "stone-furnace" in state and "burner-mining-drill" in state
    assert sum(m["role"] == "assistant" for m in last) == 3          # only the window


def test_batch_stops_after_first_failure(http, token):
    run, task, constants = level1(http, token)
    model = FakeModel([
        reply(calls=[tool_call(1, "place", {"entity": "stone-furnace", "x": 500, "y": 500, "direction": "north"}),
                     tool_call(2, "insert", {"x": 500, "y": 500, "item": "coal", "count": 5}),
                     tool_call(3, "advance", {"ticks": 600})], finish="tool_calls"),
        reply(calls=[tool_call(4, "end_run", {})], finish="tool_calls"),
    ])
    result = openai_bot.play(run, task, constants, model, "fake", log=lambda *a: None)
    assert result["budgets_used"]["actions"] == 1                    # only the failed place was charged
    assert result["bot"]["skipped_calls"] == 2
    msgs = model.requests[1]["messages"]
    outs = {m["tool_call_id"]: json.loads(m["content"]) for m in msgs if m["role"] == "tool"}
    assert outs["call_1"]["ok"] is False and outs["call_2"]["skipped"] and outs["call_3"]["skipped"]


def test_memo_is_stripped_before_the_game_sees_it(settings):
    task = settings.tasks["level1-iron"].model_dump()
    tools = {t["function"]["name"]: t["function"] for t in openai_bot.openai_tools(task, strict=True, memo=True)}
    assert "memo" in tools["place"]["parameters"]["properties"]
    assert "memo" in tools["place"]["parameters"]["required"]
    assert "memo" not in tools["end_run"]["parameters"]["properties"]


def test_map_compression_and_ledger():
    import memory
    rows = memory._rle_rows([(4, 3), (5, 3), (6, 3), (9, 3), (4, 4)])
    assert rows == ["y=3: 4-6 9", "y=4: 4"]
    led = memory.Ledger({"start_inventory": {"stone-furnace": 2, "coal": 10}}, {"actions_left": 5})
    ent = {"name": "stone-furnace", "x": 1, "y": 1, "status": "no_fuel"}
    led.record("place", {"entity": "stone-furnace", "x": 1, "y": 1, "direction": "north"},
               {"ok": True, "data": {"entity": ent}, "budgets": {"actions_left": 4}})
    led.record("insert", {"x": 1, "y": 1, "item": "coal", "count": 4},
               {"ok": True, "data": {"inserted": 4, "entity": {**ent, "status": "working"}}, "budgets": {"actions_left": 3}})
    assert led.inventory == {"stone-furnace": 1, "coal": 6}
    assert "working" in led.render()
    led.record("pickup", {"x": 1, "y": 1}, {"ok": True, "data": {"picked_up": {"stone-furnace": 1, "coal": 4}},
                                           "budgets": {"actions_left": 2}})
    assert led.entities == {} and led.inventory == {"stone-furnace": 2, "coal": 10}


def test_bot_plays_a_time_only_level(http, token):
    client = ArenaClient(token=token, http=http)
    rules = client.rules()
    task = next(t for t in rules["tasks"] if t["id"] == "level2-bootstrap-timed")
    run = client.start_run("level2-bootstrap-timed")
    model = FakeModel([
        reply(calls=[tool_call(1, "observe", {"radius": 10, "memo": "look around"}),
                     tool_call(2, "move", {"x": 1.5, "y": 0.5, "memo": "step"})], finish="tool_calls"),
        reply(calls=[tool_call(3, "end_run", {})], finish="tool_calls"),
    ])
    result = openai_bot.play(run, task, rules["constants"], model, "fake", log=lambda *a: None)
    assert result["status"] == "finished" and result["budgets_used"]["actions"] == 1
    system = model.requests[0]["messages"][0]["content"]
    assert "no action limit" in system and "at least 60 ticks" in system and "1200" in system
    state = model.requests[1]["messages"][-1]["content"]
    assert "no limit on actions" in state


def test_cost_cap_stops_before_the_bill_passes_it(http, token):
    run, task, constants = level1(http, token)
    big = NS(prompt_tokens=1_000_000, completion_tokens=0)          # $1.00 a turn on gpt-6.1-terra
    turn = [tool_call(i, "observe", {"radius": 5}) for i in range(1)]
    model = FakeModel([NS(choices=[NS(message=NS(content=None, tool_calls=turn), finish_reason="tool_calls")],
                          usage=big)] * 10)
    result = openai_bot.play(run, task, constants, model, "gpt-6.1-terra", max_cost_usd=3.5, log=lambda *a: None)
    assert result["status"] == "finished"
    assert result["bot"]["stop"] == "cost_cap" and result["bot"]["turns"] == 2
    assert result["bot"]["cost_usd"] == 2.0 <= 3.5


def test_cost_cap_needs_a_known_price(http, token):
    run, task, constants = level1(http, token)
    with pytest.raises(ValueError):
        openai_bot.play(run, task, constants, FakeModel([]), "mystery-model", max_cost_usd=1, log=lambda *a: None)
    run.end_and_wait()


def test_model_that_refuses_tools_with_reasoning_is_retried_with_none(http, token):
    run, task, constants = level1(http, token)
    refusal = RuntimeError("Error code: 400 - Function tools with reasoning_effort are not supported for "
                           "gpt-6.1-terra in /v1/chat/completions. ... set reasoning_effort to 'none'.")
    model = FakeModel([refusal, reply(calls=[tool_call(1, "end_run", {})])])
    result = openai_bot.play(run, task, constants, model, "gpt-6.1-terra", log=lambda *a: None)
    assert result["bot"]["stop"] == "end_run"
    assert model.requests[-1]["reasoning_effort"] == "none"


def test_ledger_counts_a_belt_line_from_the_placed_tiles():
    from memory import Ledger
    led = Ledger({"start_inventory": {"transport-belt": 20}}, {})
    out = {"ok": True, "budgets": {}, "data": {"placed": [[0.5, 0.5], [0.5, 1.5], [0.5, 2.5]], "direction": "south"}}
    led.record("place_line", {"entity": "transport-belt", "x1": 0.5, "y1": 0.5, "x2": 0.5, "y2": 2.5}, out)
    assert led.inventory["transport-belt"] == 17 and "transport-belt" in led.render()


def test_responses_adapter_plays_a_run(http, token):
    """The Responses API path: tools + reasoning, earlier calls sent back as text, usage mapped."""
    run, task, constants = level1(http, token)
    sent = []

    def fake_create(**kw):
        sent.append(json.loads(json.dumps(kw)))
        if len(sent) == 1:
            out = [NS(type="reasoning"), NS(type="function_call", call_id="c1", name="observe", arguments='{"radius": 5}')]
        else:
            out = [NS(type="message", content=[NS(type="output_text", text="done")]),
                   NS(type="function_call", call_id="c2", name="end_run", arguments="{}")]
        return NS(output=out, usage=NS(input_tokens=1000, output_tokens=200,
                                       input_tokens_details=NS(cached_tokens=400)))

    llm = openai_bot.ResponsesAdapter(NS(responses=NS(create=fake_create)), "low")
    result = openai_bot.play(run, task, constants, llm, "gpt-6.1-sol", max_cost_usd=1, log=lambda *a: None)
    assert result["bot"]["stop"] == "end_run" and result["bot"]["turns"] == 2
    first, second = sent
    assert first["reasoning"] == {"effort": "low"} and first["store"] is False
    assert "Factorio" in first["instructions"] and all(i["role"] in ("user", "assistant") for i in first["input"])
    assert all(t["type"] == "function" and "name" in t and t["strict"] is False for t in first["tools"])
    assert any("observe" in str(i["content"]) for i in second["input"])        # the earlier call, as text
    assert result["bot"]["prompt_tokens"] == 2000 and result["bot"]["cached_tokens"] == 800
    assert result["bot"]["cost_usd"] == round((1200 * 2 + 800 * 0.1 + 400 * 10) / 1e6, 4)


def test_early_end_run_is_confirmed_and_turns_are_transcribed(http, token):
    run, task, constants = level1(http, token)
    model = FakeModel([reply(calls=[tool_call(1, "end_run", {})]), reply(calls=[tool_call(2, "end_run", {})])])
    notes = []
    result = openai_bot.play(run, task, constants, model, "fake", log=lambda *a: None, transcript=notes.append,
                             confirm_early_end=0.25)
    assert result["bot"]["stop"] == "end_run" and result["bot"]["turns"] == 2      # the first end_run was questioned
    assert "Not ended yet" in notes[0]["results"][0] and notes[1]["calls"][0]["name"] == "end_run"
