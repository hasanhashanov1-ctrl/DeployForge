TRUNCATION_MARKER = "\n--- log truncated by DeployForge ---\n"


class LogBuffer:
    def __init__(self, limit_bytes: int, initial: str = "") -> None:
        self.limit_bytes = limit_bytes
        self._value = initial
        self.truncated = len(initial.encode("utf-8")) >= limit_bytes

    @property
    def value(self) -> str:
        return self._value

    def append(self, message: str) -> None:
        if self.truncated or not message:
            return
        current = self._value.encode("utf-8")
        remaining = self.limit_bytes - len(current)
        encoded = message.encode("utf-8", errors="replace")
        if len(encoded) <= remaining:
            self._value += message
            return
        marker = TRUNCATION_MARKER.encode()
        content_space = max(0, remaining - len(marker))
        clipped = encoded[:content_space].decode("utf-8", errors="ignore")
        self._value += clipped + TRUNCATION_MARKER
        self.truncated = True


def bounded_text(value: str, limit_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit_bytes:
        return value, False
    marker = TRUNCATION_MARKER.encode()
    keep = max(0, limit_bytes - len(marker))
    tail = encoded[-keep:].decode("utf-8", errors="ignore") if keep else ""
    return TRUNCATION_MARKER + tail, True
