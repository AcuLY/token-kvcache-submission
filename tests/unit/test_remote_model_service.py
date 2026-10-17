"""SSH helper construction for the server-hosted model service (no real ssh)."""

from scripts.model_service_remote import (
    local_env,
    parse_args,
    remote_start_command,
    ssh_options,
    tunnel_command,
)


def test_ssh_options_include_only_the_requested_flags() -> None:
    assert ssh_options() == []
    assert ssh_options(port=2222) == ["-p", "2222"]
    assert ssh_options(port=2222, identity="key.pem") == ["-p", "2222", "-i", "key.pem"]


def test_tunnel_command_forwards_the_remote_model_port() -> None:
    command = tunnel_command("user@gpu-box", local_port=18100, remote_port=8100, port=22)
    assert command[0] == "ssh"
    assert command[-1] == "user@gpu-box"
    assert "18100:127.0.0.1:8100" in command
    assert "-N" in command
    assert "ExitOnForwardFailure=yes" in command


def test_remote_start_command_launches_the_real_backend_in_background() -> None:
    command = remote_start_command(
        "gpu-box",
        remote_dir="/srv/token-kvcache",
        remote_python="/srv/venv/bin/python",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        dtype="bfloat16",
        device="cuda",
        revision="main",
        remote_port=8100,
    )
    assert command[0] == "ssh"
    assert command[-2] == "gpu-box"
    inner = command[-1]
    assert "cd /srv/token-kvcache" in inner
    assert "MODEL_BACKEND=huggingface" in inner
    assert "HF_MODEL_ID=Qwen/Qwen2.5-1.5B-Instruct" in inner
    assert "HF_DTYPE=bfloat16" in inner
    assert "HF_DEVICE=cuda" in inner
    assert "HF_MODEL_REVISION=main" in inner
    assert "MODEL_SERVICE_HOST=127.0.0.1" in inner
    assert "MODEL_SERVICE_PORT=8100" in inner
    assert "nohup /srv/venv/bin/python -m model_service" in inner
    assert "> model-service.log 2>&1" in inner


def test_remote_start_command_quotes_spaces_in_the_model_id() -> None:
    command = remote_start_command(
        "gpu-box",
        remote_dir="/srv/token-kvcache",
        model="org/model with spaces",
        dtype="float32",
        device="cuda",
    )
    assert "HF_MODEL_ID='org/model with spaces'" in command[-1]


def test_local_env_selects_the_huggingface_backend() -> None:
    args = parse_args(["--local", "--model", "Qwen/Qwen2.5-0.5B-Instruct", "--dtype", "float16"])
    env = local_env(args)
    assert env["MODEL_BACKEND"] == "huggingface"
    assert env["HF_MODEL_ID"] == "Qwen/Qwen2.5-0.5B-Instruct"
    assert env["HF_DTYPE"] == "float16"
    assert env["MODEL_SERVICE_PORT"] == "8100"


def test_parse_args_requires_a_host_for_remote_mode() -> None:
    try:
        parse_args([])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - the parser must reject a missing host
        raise AssertionError("parse_args accepted remote mode without --host")
