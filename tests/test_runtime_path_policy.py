# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from mclaw.runtime.base import Runtime
from mclaw.runtime.kaihong import KaihongRuntime
from mclaw.runtime.linux import LinuxRuntime
from mclaw.runtime.paths import PathPolicy
from mclaw.runtime.windows import WindowsRuntime
from mclaw.safety.path_resolver import (
    extract_mutation_target_actions_from_command,
    extract_mutation_targets_from_command,
)
from mclaw.safety.safety_layer import MClawSafetyLayer
from mclaw.tools.terminal_tool import _credential_file_terminal_error


def test_full_host_access_keeps_credentials_scoped(tmp_path: Path) -> None:
    policy = PathPolicy(mclaw_home=tmp_path / ".mclaw")

    for action in ("read", "write", "execute"):
        decision = policy.check(action, tmp_path / "outside" / "file.txt")
        assert decision.allowed
        assert decision.scope == "host"

    for name in (".env", ".env.local", ".env_secret.tmp"):
        assert not policy.check("read", tmp_path / name).allowed
    assert not policy.check("read", Path("/proc/self/environ")).allowed

    runtime = object.__new__(Runtime)
    runtime.paths = policy
    assert "EXAMPLE_API_KEY" not in runtime.build_env({"EXAMPLE_API_KEY": "secret"})
    assert runtime.build_env(
        {"EXAMPLE_API_KEY": "secret"},
        allowed_sensitive={"EXAMPLE_API_KEY"},
    )["EXAMPLE_API_KEY"] == "secret"

    assert _credential_file_terminal_error("Get-Content .env")
    assert _credential_file_terminal_error("cat /project/.env.local")
    assert _credential_file_terminal_error("cat /proc/$PPID/environ")
    assert _credential_file_terminal_error("echo healthy") is None


@pytest.mark.parametrize(
    "relative_path",
    (
        ".env.production",
        ".ssh/id_rsa",
        ".ssh/id_ed25519",
        ".aws/credentials",
        ".kube/config",
        ".docker/config.json",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
    ),
)
def test_common_credential_files_are_denied(tmp_path: Path, relative_path: str) -> None:
    policy = PathPolicy(mclaw_home=tmp_path / ".mclaw")

    for action in ("read", "write", "delete", "execute"):
        decision = policy.check(action, tmp_path / relative_path)
        assert not decision.allowed
        assert decision.scope == "credential"

    assert policy.check("read", tmp_path / ".ssh" / "id_ed25519.pub").allowed
    assert policy.check("read", tmp_path / "proc" / "self" / "environ").allowed
    assert not policy.check("read", Path("/proc/self/environ")).allowed
    assert not policy.check("read", Path("/proc/123/mem")).allowed
    assert not policy.check("read", Path("/proc/kcore")).allowed


def test_credential_guard_checks_raw_and_symlink_resolved_paths(tmp_path: Path, monkeypatch) -> None:
    policy = PathPolicy(mclaw_home=tmp_path / ".mclaw")
    benign = tmp_path / "benign.txt"
    secret = tmp_path / ".env.secret"
    benign.write_text("ok", encoding="utf-8")
    secret.write_text("secret", encoding="utf-8")
    raw_secret_link = tmp_path / ".env.link"
    resolved_secret_link = tmp_path / "ordinary-link"
    try:
        raw_secret_link.symlink_to(benign)
        resolved_secret_link.symlink_to(secret)
    except OSError:
        assert not policy.check("read", raw_secret_link).allowed
        original_normalize = policy.normalize

        def normalize_with_simulated_link(path_value, base=None):
            if Path(path_value) == resolved_secret_link:
                return secret.resolve()
            return original_normalize(path_value, base=base)

        monkeypatch.setattr(policy, "normalize", normalize_with_simulated_link)

    assert not policy.check("read", raw_secret_link).allowed
    assert not policy.check("read", resolved_secret_link).allowed


def test_runtime_internal_is_diagnostic_read_only(tmp_path: Path) -> None:
    home = tmp_path / ".mclaw"
    policy = PathPolicy(mclaw_home=home)
    state_file = home / "sessions" / "session.json"

    for action in ("read", "list", "search", "execute"):
        decision = policy.check(action, state_file)
        assert decision.allowed
        assert decision.scope == "runtime_internal"

    for action in ("write", "delete", "move", "move_destination", "copy", "overwrite"):
        decision = policy.check(action, state_file)
        assert not decision.allowed
        assert decision.scope == "runtime_internal"


def test_exact_anchors_do_not_make_descendants_read_only(tmp_path: Path) -> None:
    anchor = tmp_path / "protected"
    policy = PathPolicy(
        mclaw_home=tmp_path / ".mclaw",
        protected_anchors=(anchor,),
    )

    assert not policy.check("delete", anchor).allowed
    assert not policy.check("move", anchor).allowed
    assert not policy.check("overwrite", anchor).allowed
    assert policy.check("copy", anchor).allowed
    assert policy.check("move_destination", anchor).allowed
    assert policy.check("write", anchor / "normal" / "file.txt").allowed
    assert policy.check("delete", anchor / "normal" / "file.txt").allowed


def test_pseudo_filesystems_allow_explicit_io_but_not_search_or_removal(tmp_path: Path) -> None:
    pseudo = tmp_path / "sys"
    policy = PathPolicy(
        mclaw_home=tmp_path / ".mclaw",
        pseudo_roots=(pseudo,),
    )
    node = pseudo / "class" / "leds" / "brightness"

    assert policy.check("read", node).allowed
    assert policy.check("write", node).allowed
    assert not policy.check("delete", node).allowed
    assert not policy.check("move", node).allowed
    assert not policy.check("search", pseudo).allowed
    assert not policy.check("search", tmp_path).allowed


def test_runtime_profiles_supply_os_specific_exact_anchors() -> None:
    windows = WindowsRuntime().paths
    linux = LinuxRuntime().paths
    kaihong = KaihongRuntime().paths

    assert not windows.check("delete", Path.home()).allowed
    assert windows.check("write", Path.home() / "project" / "file.txt").allowed

    assert not linux.check("delete", Path("/etc")).allowed
    assert linux.check("write", Path("/etc") / "mclaw-example.conf").allowed
    assert not linux.check("search", Path("/proc")).allowed

    assert not kaihong.check("delete", Path("/data")).allowed
    assert not kaihong.check("move", Path("/data/local/release")).allowed
    assert kaihong.check("write", Path("/data/local/release/bin/mclaw-helper")).allowed
    assert kaihong.check("write", Path("/data/local/tmp/mclaw_src/main.py")).allowed
    assert kaihong.check("write", Path("/data/acs/acs/file_sharing/documents/report.md")).allowed


def test_terminal_mutation_targets_cover_posix_and_relative_paths() -> None:
    assert extract_mutation_targets_from_command("rm /tmp/file.txt") == ["/tmp/file.txt"]
    assert extract_mutation_targets_from_command("rm relative.txt") == ["relative.txt"]
    assert extract_mutation_targets_from_command("cp source.txt output/result.txt") == ["output/result.txt"]
    assert extract_mutation_targets_from_command("mv old.txt new.txt") == ["old.txt", "new.txt"]
    assert extract_mutation_target_actions_from_command("mv old.txt new.txt") == [
        ("move", "old.txt"),
        ("move_destination", "new.txt"),
    ]
    assert extract_mutation_target_actions_from_command("mv -t output one.txt two.txt") == [
        ("move", "one.txt"),
        ("move", "two.txt"),
        ("move_destination", "output"),
    ]
    assert extract_mutation_targets_from_command("echo ok > logs/run.txt") == ["logs/run.txt"]
    assert extract_mutation_targets_from_command("rm one; rm two") == ["one", "two"]
    assert extract_mutation_targets_from_command('rm "$UNKNOWN_MCLAW_TARGET"') == []


def test_safety_layer_uses_path_policy_after_command_shape_checks(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor"
    policy = PathPolicy(
        mclaw_home=tmp_path / ".mclaw",
        protected_anchors=(anchor,),
    )
    layer = MClawSafetyLayer(path_policy=policy)

    blocked_anchor = layer.plan(
        "terminal",
        {"command": "rm anchor", "workdir": str(tmp_path)},
    )
    assert not blocked_anchor.decision.allowed
    assert "exact filesystem anchor" in blocked_anchor.decision.reason

    allowed_file = layer.plan(
        "terminal",
        {"command": "rm ordinary.txt", "workdir": str(tmp_path)},
    )
    assert allowed_file.decision.allowed

    move_into_anchor = layer.plan(
        "terminal",
        {"command": "mv ordinary.txt anchor", "workdir": str(tmp_path)},
    )
    assert move_into_anchor.decision.allowed

    move_anchor_itself = layer.plan(
        "terminal",
        {"command": "mv anchor ordinary", "workdir": str(tmp_path)},
    )
    assert not move_anchor_itself.decision.allowed

    move_into_runtime_state = layer.plan(
        "terminal",
        {"command": "mv ordinary.txt .mclaw", "workdir": str(tmp_path)},
    )
    assert not move_into_runtime_state.decision.allowed

    recursive = layer.plan(
        "terminal",
        {"command": "rm -rf build", "workdir": str(tmp_path)},
    )
    assert not recursive.decision.allowed
    assert recursive.decision.reason == "recursive or wildcard delete"

    combined_recursive = layer.plan(
        "terminal",
        {"command": "rm -Rfv build", "workdir": str(tmp_path)},
    )
    assert not combined_recursive.decision.allowed
    assert combined_recursive.decision.reason == "recursive or wildcard delete"

    unresolved = layer.plan(
        "terminal",
        {"command": 'rm "$UNKNOWN_MCLAW_TARGET"', "workdir": str(tmp_path)},
    )
    assert not unresolved.decision.allowed
    assert unresolved.decision.reason == "destructive command with unresolved targets"


def test_safety_layer_resolves_file_targets_from_agent_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    layer = MClawSafetyLayer(path_policy=PathPolicy(mclaw_home=tmp_path / ".mclaw"))
    parent = type("Agent", (), {"workspace_path": str(workspace), "config": {}})()

    plan = layer.plan("write_file", {"path": "output/result.txt"}, parent)

    assert plan.target_paths == [str(workspace / "output" / "result.txt")]
    assert plan.decision.allowed


@pytest.mark.parametrize(
    "command",
    (
        "cat ~/.ssh/id_ed25519",
        "Get-Content $HOME/.aws/credentials",
        "cat ~/.kube/config",
        "type %USERPROFILE%\\.docker\\config.json",
        "cat ~/.git-credentials",
        "cat ~/.netrc",
        "cat ~/.npmrc",
        "cat ~/.pypirc",
        "cat /proc/1/mem",
        "cat /proc/kcore",
    ),
)
def test_terminal_blocks_common_credential_references(command: str) -> None:
    assert _credential_file_terminal_error(command)
