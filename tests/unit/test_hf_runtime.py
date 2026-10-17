"""Real HuggingFace KV export/import/continuation on a tiny random Llama.

No pretrained weights are downloaded. This exercises the actual tensor path of
the server-side runtime: chunked Prefill, greedy generation, range export,
import, and continuation compared against a from-scratch prefill.
"""

import base64

import pytest

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")

from model_service.hf_runtime import (  # noqa: E402
    HF_CACHE_FORMAT,
    KV_CONTAINER_BYTES,
    HuggingFaceKvModelRuntime,
)
from model_service.runtime import StateConflictError  # noqa: E402
from shared.contracts import (  # noqa: E402
    AgentProfile,
    CreateSessionRequest,
    GenerateRequest,
    GenerationConfig,
    PrefillRequest,
    RenderLocalInputRequest,
    SealRequest,
    TokenizeRequest,
)
from shared.kv import (  # noqa: E402
    CacheExportRequest,
    CacheImportRequest,
    CacheStatusRequest,
    TokenImportRequest,
    prefix_digest,
    runtime_fingerprint,
)


class TinyTokenizer:
    """Deterministic character tokenizer over the model's 128-token vocabulary."""

    eos_token_id = 2
    bos_token_id = 1
    name_or_path = "tiny-tokenizer"
    init_kwargs: dict = {}

    def encode(self, text: str, add_special_tokens: bool = True) -> list[int]:
        token_ids = [3 + (ord(character) % 90) for character in text]
        return [self.bos_token_id, *token_ids] if add_special_tokens else token_ids

    def decode(self, token_ids: list[int], skip_special_tokens: bool = True) -> str:
        return "".join(chr(32 + (token % 90)) for token in token_ids)


class ChatTemplateTokenizer(TinyTokenizer):
    """Stands in for a family tokenizer that ships a chat template."""

    chat_template = "{{ stub-template }}"
    user_header = [40, 41]
    assistant_header = [42, 43]

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        tokenize: bool = True,
        add_generation_prompt: bool = False,
    ) -> list[int]:
        token_ids = [self.bos_token_id]
        for message in messages:
            token_ids += self.user_header
            token_ids += self.encode(message["content"], add_special_tokens=False)
            token_ids += [self.eos_token_id]
        if add_generation_prompt:
            token_ids += self.assistant_header
        return token_ids


def build_runtime(tokenizer: TinyTokenizer | None = None, **kwargs) -> HuggingFaceKvModelRuntime:
    from transformers import LlamaConfig, LlamaForCausalLM

    torch.manual_seed(7)
    config = LlamaConfig(
        vocab_size=128,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=512,
    )
    model = LlamaForCausalLM(config).eval()
    return HuggingFaceKvModelRuntime(
        model=model,
        tokenizer=tokenizer or TinyTokenizer(),
        prefill_chunk_tokens=4,
        device="cpu",
        **kwargs,
    )


async def new_session(runtime: HuggingFaceKvModelRuntime, agent_id: str) -> str:
    response = await runtime.create_session(
        CreateSessionRequest(agent_id=agent_id, conversation_id="conversation")
    )
    return response.session.session_id


async def seed_prefix(
    runtime: HuggingFaceKvModelRuntime,
    session_id: str,
    tokens: list[int],
    state_id: str = "shared-s0",
) -> None:
    await runtime.prefill(
        PrefillRequest(
            session_id=session_id,
            token_ids=tokens,
            state_id=state_id,
            replace=True,
            pinned_prefix_tokens=len(tokens),
        )
    )
    await runtime.seal(
        SealRequest(
            session_id=session_id,
            state_id=state_id,
            expected_token_count=len(tokens),
        )
    )


def max_abs_difference(left: list[float], right: list[float]) -> float:
    return max(abs(a - b) for a, b in zip(left, right, strict=True))


async def test_descriptor_reports_the_real_kv_layout() -> None:
    runtime = build_runtime()
    descriptor = await runtime.describe()
    assert descriptor.provider == "huggingface"
    assert descriptor.kv_cache_format == HF_CACHE_FORMAT
    assert descriptor.kv_container_bytes == KV_CONTAINER_BYTES
    # 2 layers x 2 K/V x 2 kv-heads x 8 head-dim x 4 bytes (float32)
    assert descriptor.kv_bytes_per_token == 256
    assert {"shared_context_v2", "kv_full", "kv_delta"}.issubset(descriptor.capabilities)


async def test_temperature_sampling_is_seeded_and_preserves_rng() -> None:
    runtime = build_runtime()
    results = []
    before = torch.random.get_rng_state().clone()
    for agent_id in ("agent-a", "agent-b"):
        session = await new_session(runtime, agent_id)
        await seed_prefix(runtime, session, list(range(10, 42)))
        result = await runtime.generate(
            GenerateRequest(
                session_id=session,
                turn_id=1,
                suffix_token_ids=[43, 44],
                generation_config=GenerationConfig(max_new_tokens=8, temperature=0.7, seed=23),
            )
        )
        results.append(result.generated_token_ids)
    assert results[0]
    assert results[0] == results[1]
    assert torch.equal(before, torch.random.get_rng_state())


async def test_export_size_matches_the_declared_layout() -> None:
    runtime = build_runtime()
    session = await new_session(runtime, "agent-a")
    prefix = list(range(10, 42))
    await seed_prefix(runtime, session, prefix)
    exported = await runtime.export_cache(
        CacheExportRequest(session_id=session, state_id="shared-s0", start_token=0)
    )
    descriptor = await runtime.describe()
    payload = base64.b64decode(exported.kv_base64)
    assert exported.cache_format == HF_CACHE_FORMAT
    assert len(payload) == len(prefix) * descriptor.kv_bytes_per_token + KV_CONTAINER_BYTES


async def generate_then_seal(
    runtime: HuggingFaceKvModelRuntime,
    session_id: str,
    suffix: list[int],
    max_new_tokens: int = 3,
):
    generated = await runtime.generate(
        GenerateRequest(
            session_id=session_id,
            turn_id=1,
            suffix_token_ids=suffix,
            generation_config=GenerationConfig(max_new_tokens=max_new_tokens, temperature=0.0),
        )
    )
    await runtime.seal(
        SealRequest(
            session_id=session_id,
            state_id=generated.state_id,
            expected_token_count=generated.token_count,
        )
    )
    return generated


async def continuation_reference(
    runtime: HuggingFaceKvModelRuntime,
    tokens: list[int],
    probe: list[int],
) -> list[float]:
    session = await new_session(runtime, "agent-reference")
    await runtime.prefill(
        PrefillRequest(session_id=session, token_ids=tokens, state_id="reference", replace=True)
    )
    await runtime.seal(
        SealRequest(session_id=session, state_id="reference", expected_token_count=len(tokens))
    )
    return await runtime.probe_logits(session, probe)


@pytest.mark.parametrize("mode", ["KV_FULL", "KV_DELTA", "TOKEN_DELTA"])
async def test_transfer_modes_resume_with_the_same_continuation(mode: str) -> None:
    runtime = build_runtime()
    prefix = list(range(10, 42))
    suffix = list(range(50, 58))
    probe = [70, 71]

    sender = await new_session(runtime, "agent-a")
    await seed_prefix(runtime, sender, prefix)
    generated = await generate_then_seal(runtime, sender, suffix)

    receiver = await new_session(runtime, "agent-b")
    if mode == "KV_FULL":
        exported = await runtime.export_cache(
            CacheExportRequest(session_id=sender, state_id=generated.state_id, start_token=0)
        )
        assert exported.start_token == 0
        imported = await runtime.import_cache(
            CacheImportRequest(session_id=receiver, transfer_id="t-full", payload=exported)
        )
    elif mode == "KV_DELTA":
        await seed_prefix(runtime, receiver, prefix)
        exported = await runtime.export_cache(
            CacheExportRequest(
                session_id=sender,
                state_id=generated.state_id,
                base_state_id="shared-s0",
                start_token=len(prefix),
                base_digest=prefix_digest(prefix),
                context_epoch=0,
            )
        )
        assert exported.token_ids == generated.token_ids[len(prefix) :]
        imported = await runtime.import_cache(
            CacheImportRequest(session_id=receiver, transfer_id="t-delta", payload=exported)
        )
    else:
        await seed_prefix(runtime, receiver, prefix)
        descriptor = await runtime.describe()
        manifest = _token_manifest(
            prefix,
            generated,
            "t-token",
            fingerprint=runtime_fingerprint(descriptor),
        )
        imported = await runtime.import_tokens(
            TokenImportRequest(
                session_id=receiver,
                manifest=manifest,
                token_ids=generated.token_ids[len(prefix) :],
            )
        )

    assert imported.token_ids == generated.token_ids
    actual = await runtime.probe_logits(receiver, probe)
    reference = await continuation_reference(runtime, generated.token_ids, probe)
    assert max_abs_difference(actual, reference) < 1e-4
    assert actual.index(max(actual)) == reference.index(max(reference))


def _token_manifest(prefix: list[int], generated, transfer_id: str, *, fingerprint: str):
    from shared.contracts import TransferManifest

    return TransferManifest(
        transfer_id=transfer_id,
        conversation_id="conversation",
        turn_id=1,
        source_agent="agent-a",
        target_agent="agent-b",
        mode="TOKEN",
        scope="DELTA",
        state_id=generated.state_id,
        parent_state_id=generated.parent_state_id,
        start_token=len(prefix),
        base_state_id="shared-s0",
        base_digest=prefix_digest(prefix),
        context_epoch=0,
        token_count=generated.token_count,
        payload_bytes=(generated.token_count - len(prefix)) * 4,
        network_snapshot_id="net",
        network_version=1,
        target_digest=prefix_digest(generated.token_ids),
        runtime_fingerprint=fingerprint,
    )


async def test_eviction_reports_token_and_cache_coverage_separately() -> None:
    runtime = build_runtime()
    session = await new_session(runtime, "agent-a")
    prefix = list(range(10, 42))
    await seed_prefix(runtime, session, prefix)
    generated = await generate_then_seal(runtime, session, list(range(50, 58)))
    inventory = await runtime.evict_cache(
        CacheStatusRequest(session_id=session, retain_prefix_tokens=5)
    )
    assert inventory.token_count == 5
    assert inventory.known_token_count == len(generated.token_ids)
    assert inventory.cache_format == HF_CACHE_FORMAT
    assert inventory.prefix_digest == prefix_digest(generated.token_ids[:5])


def streaming_profile(local_context: str) -> AgentProfile:
    return AgentProfile(
        agent_id="agent-a",
        display_name="Agent A",
        system_prompt="你是 Agent A。",
        role_description="提出方案。",
        local_context=local_context,
        generation_config=GenerationConfig(max_new_tokens=2, temperature=0.0),
    )


async def test_generate_accepts_the_profile_shared_local_context() -> None:
    # The default session config puts per-agent context into profile.local_context,
    # which belongs to the shared P0; only the per-turn request field is private.
    runtime = build_runtime()
    session = await new_session(runtime, "agent-a")
    prefix = list(range(10, 42))
    await seed_prefix(runtime, session, prefix)
    generated = await runtime.generate(
        GenerateRequest(
            session_id=session,
            turn_id=1,
            profile=streaming_profile("来自 Agent A 的本地上下文。"),
        )
    )
    assert generated.token_count > len(prefix)
    assert generated.generated_token_ids


async def test_generate_rejects_a_private_per_turn_local_context() -> None:
    runtime = build_runtime()
    session = await new_session(runtime, "agent-a")
    await seed_prefix(runtime, session, list(range(10, 42)))
    with pytest.raises(StateConflictError):
        await runtime.generate(
            GenerateRequest(
                session_id=session,
                turn_id=1,
                profile=streaming_profile("来自 Agent A 的本地上下文。"),
                local_context="private per-turn input",
            )
        )


async def test_prompt_format_defaults_to_plain_without_a_template() -> None:
    runtime = build_runtime()
    descriptor = await runtime.describe()
    assert descriptor.prompt_format == "plain"
    with pytest.raises(StateConflictError):
        build_runtime(prompt_format="chat_template")


async def test_turn_template_is_configurable() -> None:
    runtime = build_runtime(turn_template="[TURN {turn_id} :: {agent_id}]")
    session = await new_session(runtime, "agent-a")
    await seed_prefix(runtime, session, list(range(10, 42)))
    rendered = await runtime.render_local_input(
        RenderLocalInputRequest(
            session_id=session,
            profile=streaming_profile("来自 Agent A 的本地上下文。"),
            turn_id=3,
        )
    )
    expected = runtime._encode("[TURN 3 :: agent-a]\n", add_special_tokens=False)
    assert rendered.token_ids == expected
    with pytest.raises(ValueError):
        build_runtime(turn_template="{unknown_placeholder}")


async def test_turn_template_uses_current_public_profile_instructions() -> None:
    runtime = build_runtime(turn_template="{agent_id}: {system_prompt}")
    session = await new_session(runtime, "agent-a")
    await seed_prefix(runtime, session, list(range(10, 42)))
    profile = streaming_profile("").model_copy(update={"system_prompt": "Support cancellation"})
    first = await runtime.render_local_input(
        RenderLocalInputRequest(session_id=session, profile=profile, turn_id=1)
    )
    profile = profile.model_copy(update={"system_prompt": "Oppose cancellation"})
    second = await runtime.render_local_input(
        RenderLocalInputRequest(session_id=session, profile=profile, turn_id=2)
    )
    assert first.token_ids == runtime._encode(
        "agent-a: Support cancellation\n", add_special_tokens=False
    )
    assert second.token_ids == runtime._encode(
        "agent-a: Oppose cancellation\n", add_special_tokens=False
    )


async def test_chat_template_wraps_turns_and_keeps_the_cache_aligned() -> None:
    runtime = build_runtime(tokenizer=ChatTemplateTokenizer())
    descriptor = await runtime.describe()
    assert descriptor.prompt_format == "chat_template"

    shared = await runtime.tokenize(TokenizeRequest(text="shared context", add_bos=True))
    assert shared.token_ids[0] == ChatTemplateTokenizer.bos_token_id
    assert shared.token_ids[1:3] == ChatTemplateTokenizer.user_header
    assert shared.token_ids[-1] == ChatTemplateTokenizer.eos_token_id

    session = await new_session(runtime, "agent-a")
    await runtime.prefill(
        PrefillRequest(
            session_id=session,
            token_ids=shared.token_ids,
            state_id="s0",
            replace=True,
            pinned_prefix_tokens=len(shared.token_ids),
        )
    )
    await runtime.seal(
        SealRequest(
            session_id=session,
            state_id="s0",
            expected_token_count=len(shared.token_ids),
        )
    )

    profile = streaming_profile("来自 Agent A 的本地上下文。")
    rendered = await runtime.render_local_input(
        RenderLocalInputRequest(session_id=session, profile=profile, turn_id=1)
    )
    # A fresh user turn, no second BOS, ending on the assistant header.
    assert rendered.token_ids[:2] == ChatTemplateTokenizer.user_header
    assert rendered.token_ids[-2:] == ChatTemplateTokenizer.assistant_header

    generated = await runtime.generate(
        GenerateRequest(session_id=session, turn_id=1, profile=profile)
    )
    await runtime.seal(
        SealRequest(
            session_id=session,
            state_id=generated.state_id,
            expected_token_count=generated.token_count,
        )
    )
    # export_cache only succeeds when the cache spans the whole canonical
    # sequence, so this asserts the KV length invariant after generate().
    exported = await runtime.export_cache(
        CacheExportRequest(session_id=session, state_id=generated.state_id, start_token=0)
    )
    assert exported.token_ids == generated.token_ids
    prompt_end = len(shared.token_ids) + len(rendered.token_ids)
    assert generated.token_ids[prompt_end - len(rendered.token_ids) : prompt_end] == (
        rendered.token_ids
    )
