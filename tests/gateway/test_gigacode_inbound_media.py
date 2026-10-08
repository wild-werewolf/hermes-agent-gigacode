"""GigaCode sessions never pre-process media through another model (vision) or an STT provider."""

from __future__ import annotations

from types import SimpleNamespace

from agent.gigacode import prompt_pack
from agent.image_routing import decide_image_input_mode
from gateway.run_inbound_media import gigacode_session


def _runner(api_mode):
    return SimpleNamespace(_resolve_session_agent_runtime=lambda **kw: ("m", {"api_mode": api_mode}))


def test_images_are_never_routed_to_vision_pre_analysis():
    cfg = {"agent": {"image_input_mode": "text"}, "auxiliary": {"vision": {"provider": "openai"}}}
    assert decide_image_input_mode("gigacode-cli", "fixture-model", cfg) == "native"


def test_gigacode_session_detection_and_marker_refusal():
    assert gigacode_session(_runner("gigacode_cli"), None, "k")
    assert not gigacode_session(_runner("chat_completions"), None, "k")
    voice_turn = f"{prompt_pack.UNSUPPORTED_ATTACHMENT}\n\nчто я сказал?"
    assert prompt_pack.has_attachments(voice_turn) and not prompt_pack.has_attachments("обычный текст")
