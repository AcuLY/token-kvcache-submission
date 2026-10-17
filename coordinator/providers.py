"""Replaceable coordinator providers used by the session state machine."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

from coordinator.schemas import EnvironmentDocument, MockConfig, NetworkScenario, utc_now
from shared.interfaces import NetworkStateProvider, TransferModeHandler
from shared.models import NetworkState, StateOffer, TransferCandidate


class DirectionalMockNetworkProvider:
    provider_id = "directional_mock"
    contributes_mock_ui = True

    def __init__(self, config: MockConfig | None = None) -> None:
        self._config = config or MockConfig()

    @property
    def config(self) -> MockConfig:
        return self._config.model_copy(deep=True)

    def replace_config(self, config: MockConfig) -> MockConfig:
        self._config = config.model_copy(deep=True)
        return self.config

    def snapshot(self, source_agent: str, target_agent: str, transfer_id: str) -> NetworkState:
        direction = f"{source_agent}->{target_agent}"
        scenario = self._config.links.get(direction)
        if scenario is None:
            scenario = NetworkScenario()
        digest = hashlib.sha256(
            f"{scenario.seed}:{self._config.version}:{transfer_id}".encode()
        ).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        sampled_jitter = rng.uniform(-scenario.jitter_ms, scenario.jitter_ms)
        effective_rtt = max(0.0, scenario.rtt_ms + sampled_jitter)
        return NetworkState(
            snapshot_id=f"net-{transfer_id}",
            source_agent=source_agent,
            target_agent=target_agent,
            bandwidth_mbps=scenario.bandwidth_mbps,
            rtt_ms=scenario.rtt_ms,
            jitter_ms=scenario.jitter_ms,
            sampled_jitter_ms=sampled_jitter,
            effective_rtt_ms=effective_rtt,
            version=self._config.version,
            captured_at=utc_now(),
            seed=scenario.seed,
        )


def estimate_p2p_ms(payload_bytes: int, network: NetworkState) -> float:
    serialization_ms = payload_bytes * 8 / (network.bandwidth_mbps * 1_000_000) * 1_000
    return network.effective_rtt_ms + serialization_ms


class TokenCandidateProvider:
    """Metadata provider for Token message/recovery capabilities."""

    provider_id = "token_message"
    mode = "TOKEN"
    scope = "DELTA"

    def __init__(self, scope: str = "DELTA") -> None:
        self.scope = scope

    def candidate(self, offer: StateOffer, network: NetworkState) -> TransferCandidate:
        return TransferCandidate(
            mode=self.mode,
            scope=self.scope,
            token_count=offer.token_count,
            start_token=offer.shared_prefix_tokens or 0,
            payload_bytes=offer.payload_bytes,
            predicted_p2p_ms=estimate_p2p_ms(offer.payload_bytes, network),
        )


class KvSnapshotHandler:
    provider_id = "mock_kv"
    mode = "KV"

    def __init__(self, scope: str) -> None:
        self.scope = scope

    def candidate(self, offer: StateOffer, network: NetworkState) -> TransferCandidate:
        size = offer.token_count * 20
        return TransferCandidate(
            mode=self.mode,
            scope=self.scope,
            token_count=offer.token_count,
            payload_bytes=size,
            predicted_p2p_ms=estimate_p2p_ms(size, network),
        )


@dataclass(slots=True)
class ProviderRegistry:
    """Environment-selected providers; adding another provider does not alter the FSM."""

    environment: EnvironmentDocument
    network: NetworkStateProvider
    transfer_handlers: dict[str, TransferModeHandler]

    def transfer_candidates(
        self, offer: StateOffer, network: NetworkState
    ) -> list[TransferCandidate]:
        return [handler.candidate(offer, network) for handler in self.transfer_handlers.values()]

    def capabilities(self) -> dict[str, object]:
        ui_extensions: list[dict[str, str]] = []
        if self.network.contributes_mock_ui:
            ui_extensions.append(
                {"id": "mock-lab", "label": "Mock 实验室", "route": "/mock", "kind": "tool"}
            )
        return {
            "providers": {
                "model": self.environment.model_provider,
                "network": self.environment.network_provider,
                "transfer": self.environment.transfer_provider,
            },
            "ui_extensions": ui_extensions,
            "transfer_modes": [
                f"{handler.mode}/{handler.scope}" for handler in self.transfer_handlers.values()
            ],
        }
