"""Opt-in black-box tests for a running host or Docker stack."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

BASE_URL = os.getenv("TOKEN_KVCACHE_BASE_URL")
pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="set TOKEN_KVCACHE_BASE_URL to run the live-stack tests",
)


def _wait_until_ready(client: httpx.Client, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        agents = client.get("/api/v1/agents").raise_for_status().json()
        if len(agents) == 2 and all(agent["ready"] for agent in agents):
            return
        time.sleep(0.2)
    raise AssertionError("both agents did not become READY")


def _run_session(client: httpx.Client, config: dict[str, Any] | None = None) -> dict[str, Any]:
    _wait_until_ready(client)
    created = client.post("/api/v1/sessions", json={"config": config}).raise_for_status().json()
    conversation_id = created["conversation_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        session = client.get(f"/api/v1/sessions/{conversation_id}").raise_for_status().json()
        if session["status"] in {"COMPLETED", "ERROR", "STOPPED"}:
            return session
        time.sleep(0.1)
    raise AssertionError("session did not reach a terminal state")


def _contains_key(value: Any, forbidden: str) -> bool:
    if isinstance(value, dict):
        return forbidden in value or any(
            _contains_key(child, forbidden) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(child, forbidden) for child in value)
    return False


@pytest.mark.parametrize("policy", ["always_token", "always_kv", "adaptive"])
@pytest.mark.parametrize("first_speaker", ["agent-a", "agent-b"])
def test_shared_prompt_incremental_round_trip(policy: str, first_speaker: str) -> None:
    assert BASE_URL is not None
    with httpx.Client(base_url=BASE_URL, timeout=10, trust_env=False) as client:
        config = client.get("/api/v1/config").raise_for_status().json()["config"]
        config.update(mode_policy=policy, max_agent_turns=6, first_speaker=first_speaker)
        config["decision"]["history_window_tokens"] = None
        session = _run_session(client, config)
        assert session["status"] == "COMPLETED", session
        assert session["protocol_version"] == "2"
        initial = list(session["initialization"].values())
        assert len(initial) == 2
        assert initial[0]["known_token_digest"] == initial[1]["known_token_digest"]
        assert initial[0]["token_count"] == initial[0]["known_token_count"] > 0
        assert len(session["ledger"]) == 5
        for row in session["transfers"]:
            assert row["variant"] == "DELTA"
            assert row["xi_tokens"] == 0
            assert (
                row["token_count"]
                == row["start_token"] + row["output_token_count"] + row["wrapper_token_count"]
            )
            stride = 4 if row["mode"] == "TOKEN" else 20
            assert (
                row["payload_bytes"]
                == (row["output_token_count"] + row["wrapper_token_count"]) * stride
            )
            assert row["decision"]["current_tokens"] == row["output_token_count"]
        if policy == "always_token":
            assert all(row["mode"] == "TOKEN" for row in session["transfers"])
            assert (
                session["transfers"][-1]["payload_bytes"]
                < session["transfers"][-1]["token_count"] * 4
            )
        elif policy == "always_kv":
            assert all(row["mode"] == "KV" for row in session["transfers"])
        assert not _contains_key(session, "token_ids")
        assert not _contains_key(session, "kv_base64")
        csv = client.get("/api/v1/experiments.csv").raise_for_status().text
        assert "alpha_tokens" in csv and "paper_kv_bits" in csv


@pytest.mark.parametrize("policy", ["always_token", "always_kv"])
def test_real_cache_gap_keeps_known_tokens_and_backfills(policy: str) -> None:
    assert BASE_URL is not None
    with httpx.Client(base_url=BASE_URL, timeout=10, trust_env=False) as client:
        saved = client.get("/api/v1/mock").raise_for_status().json()
        path = None
        try:
            mock = client.get("/api/v1/mock").json()
            mock["model"]["generate_delay_ms"] = 300
            client.put("/api/v1/mock", json=mock).raise_for_status()
            _wait_until_ready(client)
            config = client.get("/api/v1/config").json()["config"]
            config.update(mode_policy=policy, max_agent_turns=4)
            config["decision"]["history_window_tokens"] = None
            created = (
                client.post("/api/v1/sessions", json={"config": config}).raise_for_status().json()
            )
            path = f"/api/v1/sessions/{created['conversation_id']}"
            client.post(f"{path}/pause").raise_for_status()

            def wait_transfer(count: int):
                for _ in range(200):
                    current = client.get(path).raise_for_status().json()
                    if len(current["ledger"]) == count and current["pending_transfer_id"] is None:
                        return current
                    assert current["status"] != "ERROR", current
                    time.sleep(0.05)
                raise AssertionError("transfer did not commit")

            first = wait_transfer(1)
            agent = next(
                a
                for a in client.get("/api/v1/agents").json()
                if a["agent_id"] == config["first_speaker"]
            )
            endpoint = agent["control_url"]
            initial_count = first["initial_state"]["known_token_count"]
            response = (
                client.post(
                    endpoint + "/internal/v1/commands",
                    json={
                        "command_id": created["conversation_id"] + ":evict",
                        "type": "evict_cache",
                        "payload": {
                            "conversation_id": created["conversation_id"],
                            "retain_prefix_tokens": initial_count,
                        },
                    },
                )
                .raise_for_status()
                .json()
            )
            assert response["accepted"]
            inventory = client.get(
                endpoint + "/internal/v1/cache-status/" + created["conversation_id"]
            ).json()
            assert inventory["known_token_count"] > inventory["token_count"] == initial_count
            client.post(f"{path}/resume").raise_for_status()
            client.post(f"{path}/pause").raise_for_status()
            second = wait_transfer(2)
            row = second["transfers"][1]
            assert row["xi_tokens"] == inventory["known_token_count"] - initial_count > 0
            assert row["variant"] == "DELTA"
            if policy == "always_token":
                assert row["start_token"] == inventory["known_token_count"]
                assert (
                    row["payload_bytes"]
                    == (row["output_token_count"] + row["wrapper_token_count"]) * 4
                )
                assert (
                    row["decision"]["candidates"][0]["prefill_tokens"]
                    > row["output_token_count"] + row["wrapper_token_count"]
                )
            else:
                assert row["start_token"] == initial_count
                assert (
                    row["payload_bytes"]
                    == (row["xi_tokens"] + row["output_token_count"] + row["wrapper_token_count"])
                    * 20
                )
            restored = client.get(
                endpoint + "/internal/v1/cache-status/" + created["conversation_id"]
            ).json()
            assert restored["known_token_count"] == restored["token_count"] == row["token_count"]
        finally:
            if path:
                client.post(f"{path}/stop")
                _wait_until_ready(client)
            saved["version"] = client.get("/api/v1/mock").json()["version"]
            client.put("/api/v1/mock", json=saved).raise_for_status()


def test_window_checkpoint_preserves_prompt_and_marks_recovery() -> None:
    assert BASE_URL is not None
    with httpx.Client(base_url=BASE_URL, timeout=10, trust_env=False) as client:
        config = client.get("/api/v1/config").raise_for_status().json()["config"]
        config.update(mode_policy="always_token", max_agent_turns=2)
        config["decision"]["history_window_tokens"] = None
        baseline = _run_session(client, config)
        prefix_count = baseline["initial_state"]["known_token_count"]
        config.update(max_agent_turns=10)
        config["decision"]["history_window_tokens"] = prefix_count + 200
        bounded = _run_session(client, config)
        assert bounded["status"] == "COMPLETED", bounded
        recovery = [row for row in bounded["transfers"] if row["variant"] == "RECOVERY"]
        assert recovery and all(row["trimmed_tokens"] > 0 for row in recovery)
        assert all(row["recovery_reason"] for row in recovery)
        assert (
            bounded["initial_state"]["known_token_digest"]
            == baseline["initial_state"]["known_token_digest"]
        )
        assert all(row["token_count"] <= prefix_count + 200 for row in bounded["transfers"])


def test_network_profile_changes_actual_p2p_time() -> None:
    assert BASE_URL is not None
    with httpx.Client(base_url=BASE_URL, timeout=10, trust_env=False) as client:
        config = client.get("/api/v1/config").raise_for_status().json()["config"]
        config["max_agent_turns"] = 2
        config["mode_policy"] = "always_token"

        mock = client.get("/api/v1/mock").raise_for_status().json()
        for link in mock["links"].values():
            link.update({"bandwidth_mbps": 100.0, "rtt_ms": 5.0, "jitter_ms": 0.0})
        client.put("/api/v1/mock", json=mock).raise_for_status()
        fast = _run_session(client, config)
        fast_p2p = next(
            metric["duration_ms"] for metric in fast["metrics"] if metric["phase"] == "p2p_ms"
        )

        mock = client.get("/api/v1/mock").raise_for_status().json()
        for link in mock["links"].values():
            link.update({"bandwidth_mbps": 0.1, "rtt_ms": 100.0, "jitter_ms": 0.0})
        client.put("/api/v1/mock", json=mock).raise_for_status()
        slow = _run_session(client, config)
        slow_p2p = next(
            metric["duration_ms"] for metric in slow["metrics"] if metric["phase"] == "p2p_ms"
        )

        assert slow_p2p > fast_p2p + 75


def test_stop_at_transfer_boundary_and_context_limit() -> None:
    assert BASE_URL is not None
    with httpx.Client(base_url=BASE_URL, timeout=10, trust_env=False) as client:
        _wait_until_ready(client)
        created = client.post("/api/v1/sessions", json={}).raise_for_status().json()
        path = f"/api/v1/sessions/{created['conversation_id']}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = client.get(path).raise_for_status().json()
            if current["pending_transfer_id"]:
                break
            time.sleep(0.02)
        assert current["pending_transfer_id"]
        stopped = client.post(f"{path}/stop").raise_for_status().json()
        assert stopped["status"] in {"STOPPING", "STOPPED"}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if client.get(path).json()["status"] == "STOPPED":
                break
            time.sleep(0.05)
        _wait_until_ready(client)
        assert client.get(path).json()["status"] == "STOPPED"
        config = client.get("/api/v1/config").json()["config"]
        config["max_context_tokens"] = (
            client.get(path).json()["initial_state"]["known_token_count"] + 10
        )
        limited = _run_session(client, config)
        assert limited["status"] == "COMPLETED"
        assert limited["finish_reason"] == "CONTEXT_LIMIT"
