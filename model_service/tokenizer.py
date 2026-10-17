"""The reversible ``mock-byte-v1`` tokenizer.

UTF-8 bytes occupy IDs 256..511, leaving the lower IDs available for stable
chat-template control tokens.  Therefore arbitrary Unicode text round-trips
exactly and no encoded byte can collide with a control marker.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import IntEnum


class ControlToken(IntEnum):
    PAD = 0
    BOS = 1
    EOS = 2
    SYSTEM_START = 10
    SYSTEM_END = 11
    USER_START = 12
    USER_END = 13
    ASSISTANT_START = 14
    ASSISTANT_END = 15


CONTROL_TOKEN_NAMES = {
    int(ControlToken.PAD): "<|pad|>",
    int(ControlToken.BOS): "<|bos|>",
    int(ControlToken.EOS): "<|eos|>",
    int(ControlToken.SYSTEM_START): "<|system_start|>",
    int(ControlToken.SYSTEM_END): "<|system_end|>",
    int(ControlToken.USER_START): "<|user_start|>",
    int(ControlToken.USER_END): "<|user_end|>",
    int(ControlToken.ASSISTANT_START): "<|assistant_start|>",
    int(ControlToken.ASSISTANT_END): "<|assistant_end|>",
}


class MockByteTokenizer:
    tokenizer_id = "mock-byte-v1"
    byte_offset = 256
    byte_limit = byte_offset + 256

    def encode(
        self,
        text: str,
        *,
        add_bos: bool = False,
        add_eos: bool = False,
    ) -> list[int]:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        tokens: list[int] = []
        if add_bos:
            tokens.append(int(ControlToken.BOS))
        tokens.extend(self.byte_offset + byte for byte in text.encode("utf-8"))
        if add_eos:
            tokens.append(int(ControlToken.EOS))
        return tokens

    def decode(
        self, token_ids: Iterable[int], *, skip_control_tokens: bool = True
    ) -> str:
        output: list[str] = []
        pending_bytes = bytearray()

        def flush_bytes() -> None:
            if pending_bytes:
                output.append(bytes(pending_bytes).decode("utf-8", errors="strict"))
                pending_bytes.clear()

        for token_id in token_ids:
            self.validate_token_id(token_id)
            if self.byte_offset <= token_id < self.byte_limit:
                pending_bytes.append(token_id - self.byte_offset)
                continue
            if not skip_control_tokens:
                flush_bytes()
                output.append(CONTROL_TOKEN_NAMES[token_id])
        flush_bytes()
        return "".join(output)

    def validate_token_id(self, token_id: int) -> None:
        if not isinstance(token_id, int) or isinstance(token_id, bool):
            raise TypeError("token IDs must be integers")
        if token_id in CONTROL_TOKEN_NAMES:
            return
        if self.byte_offset <= token_id < self.byte_limit:
            return
        raise ValueError(f"token ID {token_id} is not valid for mock-byte-v1")

    def validate(self, token_ids: Iterable[int]) -> list[int]:
        tokens = list(token_ids)
        for token_id in tokens:
            self.validate_token_id(token_id)
        return tokens


__all__ = ["CONTROL_TOKEN_NAMES", "ControlToken", "MockByteTokenizer"]

