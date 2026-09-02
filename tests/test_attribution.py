"""Tests for the prompt-component attribution."""
from __future__ import annotations

from hermes_checker.accounting import (
    ComponentAttribution,
    attribute_messages,
    classify_message_role,
)
from hermes_checker.accounting.tokenizer import Tokenizer


def test_classify_system_message() -> None:
    assert classify_message_role({"role": "system", "content": "You are a helpful assistant."}) == "SYSTEM"


def test_classify_tool_schema_message() -> None:
    msg = {"role": "system", "content": '{"tools": [{"function": {"name": "x", "parameters": {}}}]}'}
    assert classify_message_role(msg) == "TOOLS_SCHEMA"


def test_classify_user_message() -> None:
    assert classify_message_role({"role": "user", "content": "hello"}) == "USER_MESSAGES"


def test_classify_assistant_history() -> None:
    assert classify_message_role({"role": "assistant", "content": "hi"}) == "ASSISTANT_HISTORY"


def test_classify_tool_results() -> None:
    assert classify_message_role({"role": "tool", "content": "{}"}) == "TOOL_RESULTS"


def test_classify_memory_user_message() -> None:
    msg = {"role": "user", "content": "[memory] some context from earlier"}
    assert classify_message_role(msg) == "MEMORY"


def test_classify_skill_bundle() -> None:
    msg = {"role": "user", "content": "[Loaded as part of the skill bundle: foo]\nskill body"}
    assert classify_message_role(msg) == "SKILLS"


def test_classify_project_instructions() -> None:
    msg = {"role": "system", "content": "## Repository Conventions\n\n- see AGENTS.md"}
    assert classify_message_role(msg) == "PROJECT_INSTRUCTIONS"


def test_attribute_messages_buckets() -> None:
    messages = [
        {"role": "system", "content": "system instructions here " * 5},
        {"role": "system", "content": '{"tools": [{"function": {"name": "terminal"}}]}'},
        {"role": "user", "content": "hello world " * 50},
        {"role": "assistant", "content": "ok " * 30},
        {"role": "tool", "content": '{"ok": true}'},
        {"role": "user", "content": "[memory] some past note"},
    ]
    tokenizer = Tokenizer()
    components = attribute_messages(messages, tokenizer)
    names = {c.component for c in components}
    assert {"SYSTEM", "TOOLS_SCHEMA", "USER_MESSAGES", "ASSISTANT_HISTORY", "TOOL_RESULTS", "MEMORY"}.issubset(names)


def test_attribute_messages_total_tokens_within_reasonableness() -> None:
    messages = [
        {"role": "user", "content": "a" * 400},
        {"role": "user", "content": "b" * 400},
    ]
    components = attribute_messages(messages, Tokenizer())
    user = next(c for c in components if c.component == "USER_MESSAGES")
    assert user.characters == 800
    # Fallback heuristic: 800 / 4 = 200; tiktoken can be higher/lower.
    assert user.estimated_tokens > 50