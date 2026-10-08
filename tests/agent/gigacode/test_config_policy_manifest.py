"""Config validation, generated policy, argv, manifest activation gate and the bubblewrap driver plan."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent.gigacode.config import REQUIRED_MARKER, parse_settings
from agent.gigacode.driver import BubblewrapDriver, SealedFiles, load_auth_bundle
from agent.gigacode.errors import GigacodeError
from agent.gigacode.manifest import compute_facts, verify_activation
from agent.gigacode.policy import (NATIVE_DENY, SKILL_DENY, build_argv, generate_settings, mcp_config,
                                   policy_digest)
from agent.gigacode.tools import CATALOG


def test_markers_leave_the_config_unverified_not_invalid():
    settings = parse_settings({})
    assert {"verified_version", "prompt_budget_tokens", "runtime_rootfs", "executable",
            "verification_manifest", "bwrap_executable"} <= set(settings.unverified)
    with pytest.raises(GigacodeError) as exc:
        settings.require_verified()
    assert exc.value.kind == "config_unverified"


@pytest.mark.parametrize("section", [
    {"fallback": "openai"}, {"named_subagents": True}, {"execution_driver": "direct"},
    {"wall_timeout_seconds": "30"}, {"tool_policy": "everything"}, {"executable": "gigacode"},
    {"stream_protocol": "other"}, {"surprise": 1}, {"prompt_budget_tokens": 10},
])
def test_invalid_values_are_rejected_before_any_process(section):
    with pytest.raises(GigacodeError) as exc:
        parse_settings(section)
    assert exc.value.kind == "config_invalid"


def test_generated_settings_carry_one_union_deny_in_both_forms():
    settings = generate_settings(existing={"tools": {"exclude": ["custom_tool"], "allow": ["shell"]},
                                           "excludeTools": ["legacy"]})
    assert settings["tools"]["exclude"] == settings["excludeTools"]
    assert {"custom_tool", "legacy", *NATIVE_DENY, *SKILL_DENY} == set(settings["excludeTools"])
    assert "allow" not in settings["tools"] and settings["mcp"] == {"allowed": ["hermes"]}


def test_argv_is_fixed_and_carries_no_prompt_or_secret():
    argv = build_argv("/opt/gigacode/bin/gigacode", "/run/hermes/mcp.json")
    assert argv[:3] == ["/opt/gigacode/bin/gigacode", "--output-format", "stream-json"]
    assert argv.count("--output-format") == 1 and "--approval-mode=auto-edit" in argv and "--yolo" not in argv
    assert build_argv("/x", "/m", model="giga-model")[-2:] == ["--model", "giga-model"]
    with pytest.raises(ValueError):
        build_argv("/x", "/m", model="--yolo")


def test_mcp_config_lists_only_wire_names():
    cfg = mcp_config(url="http://127.0.0.1:1/mcp", token="t", wire_tools=["hermes_web_search"], tool_timeout_ms=1)
    server = cfg["mcpServers"]["hermes"]
    assert list(cfg["mcpServers"]) == ["hermes"] and server["includeTools"] == ["hermes_web_search"]
    with pytest.raises(ValueError):
        mcp_config(url="u", token="t", wire_tools=["web_search"], tool_timeout_ms=1)


def _rootfs(tmp_path: Path) -> Path:
    root = tmp_path / "rootfs"
    for rel in ("work", "home/gigacode/.gigacode", "run/hermes", "tmp", "proc", "dev", "opt/gigacode/bin", "etc"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "run/hermes/mcp.json").touch()
    (root / "opt/gigacode/bin/gigacode").write_text("#!/bin/sh\n")
    return root


def _activated(tmp_path: Path):
    root = _rootfs(tmp_path)
    bwrap = tmp_path / "bwrap"
    bwrap.write_text("bwrap")
    manifest_path = tmp_path / "manifest.json"
    section = {"executable": "/opt/gigacode/bin/gigacode", "verified_version": "1.2.3",
               "prompt_budget_tokens": 200000, "runtime_rootfs": str(root), "bwrap_executable": str(bwrap),
               "verification_manifest": str(manifest_path)}
    settings = parse_settings(section)
    facts = compute_facts(settings)
    manifest = {**facts, "verified_at": "2026-10-09T10:00:00Z", "cli": {**facts["cli"], "version": "1.2.3"},
                "model": {"id": "giga-model", "provider": "gigacode", "endpoint": "https://example.invalid"},
                "mcp": {"flags_verified": True, "native_deny_verified": True, "qualified_tool_prefix": "mcp__hermes__"},
                "os_isolation": {"checks_passed": True, "loopback_inventory_reviewed": True, "cleanup_verified": True}}
    manifest_path.write_text(json.dumps(manifest))
    return settings, manifest_path, root, manifest


def test_manifest_gate_passes_only_for_exact_pinned_facts(tmp_path):
    settings, manifest_path, root, manifest = _activated(tmp_path)
    assert verify_activation(settings).data["cli"]["version"] == "1.2.3"
    (root / "opt/gigacode/bin/gigacode").write_text("#!/bin/sh\necho swapped\n")
    with pytest.raises(GigacodeError, match="hash mismatch"):
        verify_activation(settings)


@pytest.mark.parametrize("mutate", [
    lambda m: m.pop("verified_at"),
    lambda m: m["cli"].update(version="9.9.9"),
    lambda m: m.update(policy_sha256="0" * 64),
    lambda m: m["os_isolation"].update(checks_passed=False),
    lambda m: m["mcp"].update(native_deny_verified=False),
    lambda m: m.update(schema="other"),
])
def test_manifest_mismatches_are_unverified(tmp_path, mutate):
    settings, manifest_path, _, manifest = _activated(tmp_path)
    mutate(manifest)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(GigacodeError) as exc:
        verify_activation(settings)
    assert exc.value.kind == "config_unverified"


def test_missing_manifest_and_required_markers_are_unverified(tmp_path):
    settings, manifest_path, _, _ = _activated(tmp_path)
    manifest_path.unlink()
    with pytest.raises(GigacodeError, match="unreadable"):
        verify_activation(settings)
    with pytest.raises(GigacodeError) as exc:
        verify_activation(parse_settings({"verified_version": REQUIRED_MARKER}))
    assert exc.value.kind == "config_unverified"


def test_policy_digest_pins_catalog_and_model():
    assert policy_digest(wire_catalog=CATALOG, model=None) != policy_digest(wire_catalog=CATALOG, model="m")
    assert policy_digest(wire_catalog=list(CATALOG)[:-1], model=None) != policy_digest(wire_catalog=CATALOG, model=None)


def test_bubblewrap_plan_mounts_sealed_files_read_only(tmp_path):
    root = _rootfs(tmp_path)
    driver = BubblewrapDriver(bwrap="/usr/bin/bwrap", rootfs=str(root), executable="/opt/gigacode/bin/gigacode",
                              system_settings_paths=["/etc/gigacode/settings.json"])
    sealed = SealedFiles(mcp_config={"mcpServers": {}}, settings=generate_settings(), instructions_md="x")
    descriptor = driver.prepare("gc_1", tmp_path / "run", sealed, {"oauth_creds.json": b"{}"},
                                build_argv("/opt/gigacode/bin/gigacode", "/run/hermes/mcp.json"))
    argv = list(descriptor.argv)
    for flag in ("--unshare-user", "--unshare-pid", "--die-with-parent", "--new-session"):
        assert flag in argv
    assert argv[argv.index("--ro-bind") + 1:argv.index("--ro-bind") + 3] == [str(root), "/"]
    ro_targets = [argv[i + 2] for i, a in enumerate(argv) if a == "--ro-bind"]
    assert {"/work/.gigacode", "/work/GIGACODE.md", "/run/hermes/mcp.json", "/home/gigacode/.gigacode"} <= set(ro_targets)
    assert descriptor.env["HOME"] == "/home/gigacode" and "TELEGRAM_BOT_TOKEN" not in descriptor.env
    sealed_mcp = tmp_path / "run" / "sealed" / "mcp.json"
    assert oct(sealed_mcp.stat().st_mode & 0o777) == "0o600"
    assert (tmp_path / "run" / "sealed" / "user-gigacode" / "oauth_creds.json").read_bytes() == b"{}"


def test_rootfs_system_settings_must_match_policy(tmp_path):
    root = _rootfs(tmp_path)
    (root / "etc/gigacode").mkdir()
    (root / "etc/gigacode/settings.json").write_text(json.dumps({"mcpServers": {"foreign": {}}}))
    driver = BubblewrapDriver(bwrap="/b", rootfs=str(root), executable="/opt/gigacode/bin/gigacode",
                              system_settings_paths=["/etc/gigacode/settings.json"])
    with pytest.raises(GigacodeError, match="differ"):
        driver.check_rootfs(generate_settings())
    (root / "run/hermes/mcp.json").write_text("{}")
    with pytest.raises(GigacodeError, match="mount target"):
        BubblewrapDriver(bwrap="/b", rootfs=str(root), executable="/x").check_rootfs(generate_settings())


def test_auth_bundle_accepts_only_the_manifest_allowlist(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "oauth_creds.json").write_bytes(b"creds")
    entry = {"path": "oauth_creds.json", "size": 5, "sha256": hashlib.sha256(b"creds").hexdigest()}
    assert load_auth_bundle(str(bundle), [entry]) == {"oauth_creds.json": b"creds"}
    for extra in ("settings.json", "skills/x.md", "GIGACODE.md", "unlisted.txt"):
        target = bundle / extra
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
        with pytest.raises(GigacodeError) as exc:
            load_auth_bundle(str(bundle), [entry])
        assert exc.value.kind == "config_unverified"
        target.unlink()
        if target.parent != bundle:
            target.parent.rmdir()
    (bundle / "oauth_creds.json").write_bytes(b"other")
    with pytest.raises(GigacodeError, match="differs"):
        load_auth_bundle(str(bundle), [entry])


def test_settings_come_from_the_profile_config_yaml(tmp_path, monkeypatch):
    """The real loader against a temp HERMES_HOME: the gigacode: section reaches the runtime settings."""
    from agent.gigacode.config import load_settings

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "model:\n  provider: gigacode-cli\n  api_mode: gigacode_cli\n"
        "gigacode:\n  executable: /opt/gigacode/bin/gigacode\n  wall_timeout_seconds: 900\n"
        "  skills_allowlist: [research]\n", encoding="utf-8")
    settings = load_settings()
    assert settings.executable == "/opt/gigacode/bin/gigacode" and settings.wall_timeout_seconds == 900
    assert settings.skills_allowlist == ("research",) and "verified_version" in settings.unverified
