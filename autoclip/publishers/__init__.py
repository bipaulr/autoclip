"""Publishers (plan prompt 11): one interface, two platforms (YouTube, Instagram)."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class PublishError(Exception):
    """Message never contains a token."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class QuotaFull(PublishError):
    pass


@dataclass
class PublishRequest:
    path: Path
    title: str
    description: str
    hashtags: list[str]
    privacy: str = "public"  # public | private | unlisted
    thumbnail: Path | None = None
    duration_s: float | None = None


@dataclass
class PublishResult:
    external_id: str
    url: str
    status: str  # posted | locked_private
    privacy: str
    responses: list = field(default_factory=list)


class Publisher(Protocol):
    platform: str

    def publish(self, req: PublishRequest) -> PublishResult: ...


class MockPublisher:
    def __init__(self, platform: str, result: PublishResult | None = None, error: PublishError | None = None):
        self.platform, self.result, self.error = platform, result, error
        self.requests: list[PublishRequest] = []

    def publish(self, req: PublishRequest) -> PublishResult:
        self.requests.append(req)
        if self.error:
            raise self.error
        return self.result or PublishResult(f"{self.platform}-{len(self.requests)}",
                                            f"https://example.com/{self.platform}/{len(self.requests)}",
                                            "posted", req.privacy)
