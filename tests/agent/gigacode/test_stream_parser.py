"""maestro_stream_json_v1 parser contract (appendix A fixtures; synthetic, not live GigaCode output)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.gigacode.stream_parser import LINE_LIMIT_BYTES, StreamParser

FIXTURES = Path(__file__).parent / "fixtures"
TOOLS = frozenset({"mcp__hermes__hermes_web_search", "mcp__hermes__hermes_web_extract"})


def _run(name_or_bytes, exit_code=0, *, chunk=None, **flags):
    data = name_or_bytes if isinstance(name_or_bytes, bytes) else (FIXTURES / name_or_bytes).read_bytes()
    parser = StreamParser(allowed_tools=TOOLS)
    if chunk:
        for start in range(0, len(data), chunk):
            parser.feed(data[start:start + chunk])
    else:
        parser.feed(data)
    parser.finish()
    return parser.outcome(exit_code, **flags)


def test_success_uses_terminal_usage_not_the_sum_of_partials():
    out = _run("success_with_tool.jsonl")
    assert out.completed and out.final_text == "Ответ на основе найденной страницы."
    assert out.usage == {"input_tokens": 30, "output_tokens": 7, "cache_read_input_tokens": 5, "total_tokens": 37}
    assert out.usage_partial is None and out.total_cost_usd == 0.001
    assert out.model == "fixture-model" and out.cli_session_id == "fixture-session"
    [obs] = out.observations
    assert (obs.call_id, obs.result, obs.is_error) == ("call-1", "Found one page", False)


@pytest.mark.parametrize("chunk", [1, 3, 7])
def test_fragmented_utf8_and_crlf_parse_identically(chunk):
    data = (FIXTURES / "success_with_tool.jsonl").read_bytes().replace(b"\n", b"\r\n")
    out = _run(data, chunk=chunk)
    assert out.completed and out.final_text == "Ответ на основе найденной страницы."


def test_explicit_error_is_not_success():
    out = _run("explicit_error.jsonl")
    assert not out.completed and out.error_kind == "cli_error"
    assert "fixture authorization failure" in out.error_message


@pytest.mark.parametrize("name", ["false_success.jsonl", "false_success_result_only.jsonl"])
def test_api_error_behind_success_and_exit_zero_is_an_error(name):
    out = _run(name, 0)
    assert not out.completed and out.error_kind == "api_error" and out.final_text is None


def test_quoted_api_marker_stays_a_normal_answer():
    out = _run("quoted_api_marker.jsonl")
    assert out.completed and out.final_text.startswith("В логе встречается")


def test_multiple_tool_uses_pair_by_id_not_by_order():
    out = _run("multi_tool_reordered.jsonl")
    assert out.completed and out.final_text == "Готово."  # empty result string → last assistant text
    pairs = {o.call_id: o.result for o in out.observations}
    assert pairs == {"a": "search hits", "b": "page body"}
    assert out.unmatched_results == [{"tool_use_id": "zzz", "content": "orphan"}]
    assert "private" not in json.dumps([o.__dict__ for o in out.observations]) and "private" not in out.final_text


def test_missing_result_is_partial_with_partial_usage():
    out = _run("missing_result.jsonl")
    assert not out.completed and out.error_kind == "protocol_missing_result"
    assert out.partial_text == "частичный ответ" and out.usage is None
    assert out.usage_partial == {"input_tokens": 4, "output_tokens": 2, "cache_read_input_tokens": None,
                                 "total_tokens": None}


@pytest.mark.parametrize("name, kind", [
    ("repeated_result.jsonl", "protocol_error"),
    ("trailing_after_result.jsonl", "protocol_error"),
    ("invalid_json.jsonl", "protocol_error"),
    ("unknown_type.jsonl", "protocol_error"),
    ("truncated_tail.jsonl", "protocol_truncated"),
    ("empty_final.jsonl", "empty_final_response"),
])
def test_protocol_failures(name, kind):
    out = _run(name)
    assert not out.completed and out.error_kind == kind


def test_oversized_line_is_a_protocol_limit():
    big = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * LINE_LIMIT_BYTES}]}})
    out = _run(big.encode() + b"\n")
    assert out.error_kind == "protocol_limit"


def test_empty_output_and_nonzero_exit():
    assert _run(b"", 0).error_kind == "protocol_empty_output"
    assert _run(b"", 3).error_kind == "nonzero_exit"
    out = _run("success_with_tool.jsonl", 1)
    assert not out.completed and out.error_kind == "nonzero_exit"


def test_missing_terminal_usage_is_unknown_not_zero():
    out = _run("missing_usage.jsonl")
    assert out.completed and out.usage is None  # no terminal usage → unknown, never 0
    # Assistant-event usage is kept separately and labelled partial; unreported fields stay None.
    assert out.usage_partial == {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": None,
                                 "total_tokens": None}


def test_cancellation_and_timeout_never_succeed_even_after_a_result():
    assert _run("success_with_tool.jsonl", 0, cancelled=True).error_kind == "cancelled"
    assert _run("success_with_tool.jsonl", 0, timed_out=True).error_kind == "timed_out"


def test_ungranted_tool_use_is_a_policy_violation():
    line = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "x", "name": "run_shell_command",
                                                          "input": {}}]}}
    out = _run((json.dumps(line) + "\n").encode())
    assert out.error_kind == "tool_policy_violation"


def test_init_catalog_outside_the_grant_is_a_policy_violation():
    line = {"type": "system", "subtype": "init", "tools": ["mcp__hermes__hermes_web_search", "write_file"]}
    out = _run((json.dumps(line) + "\n").encode())
    assert out.error_kind == "tool_policy_violation" and "write_file" in out.error_message
