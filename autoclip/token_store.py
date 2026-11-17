"""Where rotating secrets live. Secrets stay in env only, so the real store is the .env file."""
from pathlib import Path
from typing import Protocol


class TokenStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, **pairs: str) -> None: ...


class MemoryTokenStore:
    """Mock for tests."""

    def __init__(self, **initial: str):
        self.values = dict(initial)

    def get(self, key: str) -> str | None:
        return self.values.get(key) or None

    def set(self, **pairs: str) -> None:
        self.values.update(pairs)


class EnvFileTokenStore:
    """Reads and updates KEY=VALUE lines in a .env file, leaving every other line untouched.

    Reads the file each time so a token refreshed by another process is picked up.
    Writes in place (not rename) because the file is bind-mounted into containers.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []

    @staticmethod
    def _key(line: str) -> str | None:
        if "=" in line and not line.lstrip().startswith("#"):
            return line.split("=", 1)[0].strip()
        return None

    def get(self, key: str) -> str | None:
        for line in self._lines():
            if self._key(line) == key:
                return line.split("=", 1)[1].strip() or None
        return None

    def set(self, **pairs: str) -> None:
        remaining = dict(pairs)
        lines = self._lines()
        for i, line in enumerate(lines):
            k = self._key(line)
            if k in remaining:
                lines[i] = f"{k}={remaining.pop(k)}"
        lines += [f"{k}={v}" for k, v in remaining.items()]
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
