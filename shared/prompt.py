"""One canonical initial prompt shared by both logical model sessions."""

import json

from shared.contracts import SessionConfig


def build_shared_prompt(config: SessionConfig) -> str:
    roles = []
    for profile in (config.agent_a_profile, config.agent_b_profile):
        if profile.local_context_provider != "static":
            raise ValueError("shared-context v2 requires synchronized static context")
        roles.append(
            {
                "agent_id": profile.agent_id,
                "instructions": profile.system_prompt,
                "role": profile.role_description,
                "shared_context": profile.local_context,
            }
        )
    return "Shared discussion v2\n" + json.dumps(
        {
            "task": config.initial_input,
            "roles": roles,
            "rule": "Take turns as the named agent. All context is shared.",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
