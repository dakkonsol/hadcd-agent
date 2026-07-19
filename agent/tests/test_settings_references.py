"""Regression guard — every settings attribute agent.py reads must exist.

The first public-release review found a startup crash: Agent.run() read
`s.node_token`, which is an AgentState field, not an AgentSettings one.
Pydantic models raise AttributeError on unknown attributes, so a single
bad reference kills the agent before any loop starts — and no unit test
exercised that construction path.

This test statically walks agent.py and asserts every attribute read off
the settings object (bound as `s = self.settings`, the `s: AgentSettings`
helper parameter, or `self.settings.<attr>` directly) names a real
AgentSettings field, so the whole class of bug fails in the test suite
rather than at boot.
"""

from __future__ import annotations

import ast
import inspect

from agent import agent as agent_module
from agent.config import AgentSettings


def _binds_s_to_settings(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if this function's `s` is the settings object.

    Either `s = self.settings` appears in the body, or the function takes
    a parameter `s` annotated as AgentSettings.
    """
    for arg in fn.args.args:
        if arg.arg == "s" and isinstance(arg.annotation, ast.Name):
            if arg.annotation.id == "AgentSettings":
                return True
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "s" for t in node.targets
        ):
            continue
        v = node.value
        if (
            isinstance(v, ast.Attribute)
            and v.attr == "settings"
            and isinstance(v.value, ast.Name)
            and v.value.id == "self"
        ):
            return True
    return False


def _settings_attribute_reads() -> set[str]:
    source = inspect.getsource(agent_module)
    tree = ast.parse(source)
    reads: set[str] = set()

    # `self.settings.foo` anywhere in the module.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "settings"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
        ):
            reads.add(node.attr)

    # `s.foo`, but only inside functions where `s` is the settings object
    # (elsewhere `s` may be e.g. a socket in _get_local_ip).
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _binds_s_to_settings(fn):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "s"
            ):
                reads.add(node.attr)
    return reads


def test_agent_only_reads_real_settings_fields():
    known = set(AgentSettings.model_fields)
    # Properties and methods defined on the class are valid reads too.
    known |= {name for name in dir(AgentSettings) if not name.startswith("_")}

    unknown = _settings_attribute_reads() - known
    assert not unknown, (
        "agent.py reads settings attributes that AgentSettings does not "
        f"define: {sorted(unknown)} — each of these raises AttributeError "
        "at startup"
    )


def test_node_token_is_state_not_settings():
    """The specific reviewed bug: node_token belongs to AgentState only."""
    assert "node_token" not in AgentSettings.model_fields
    assert "node_token" not in _settings_attribute_reads()
