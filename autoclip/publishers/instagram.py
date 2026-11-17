"""Instagram Reels through the Graph API, uploading the local file (no public hosting needed).

Flow: check content_publishing_limit -> create REELS container with upload_type=resumable ->
send the bytes to the rupload URI -> poll status_code until FINISHED -> media_publish -> permalink.
"""
import time
from typing import Callable

import httpx

from autoclip.publishers import PublishError, PublishRequest, PublishResult, QuotaFull


class InstagramPublisher:
    platform = "instagram"

    def __init__(self, user_id: str, token: str, host: str = "graph.facebook.com",
                 client: httpx.Client | None = None, sleep: Callable = time.sleep,
                 poll_seconds: float = 5, max_polls: int = 60, reencode: Callable | None = None,
                 upload_attempts: int = 5, max_seconds: float = 0, trim: Callable | None = None):
        self.user_id, self.token, self.base = user_id, token, f"https://{host}"
        self.upload_attempts = max(1, upload_attempts)
        # (src, dst, seconds) -> dst; Instagram refuses longer Reels from this account
        self.max_seconds, self.trim = max_seconds, trim
        self.reencode = reencode  # (src, dst) -> dst; used once if Instagram refuses to process a file
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, write=600, read=600))
        self.sleep, self.poll_seconds, self.max_polls = sleep, poll_seconds, max_polls

    def _call(self, method: str, path: str, responses: list, **kw) -> dict:
        try:
            r = self.client.request(method, self.base + path, **kw)
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise PublishError(f"Instagram request failed ({type(e).__name__})", retryable=True) from None
        responses.append({"path": path.split("?")[0], "status_code": r.status_code, "body": body})
        if r.status_code != 200:
            err = body.get("error") or {}
            raise PublishError(f"Instagram HTTP {r.status_code}: {err.get('message', '')[:200]}",
                               retryable=r.status_code >= 500 or err.get("is_transient", False))
        return body

    def quota_left(self, responses: list | None = None) -> int:
        d = self._call("GET", f"/{self.user_id}/content_publishing_limit", responses if responses is not None else [],
                       params={"fields": "quota_usage,config", "access_token": self.token})
        row = (d.get("data") or [{}])[0]
        return int((row.get("config") or {}).get("quota_total", 50)) - int(row.get("quota_usage", 0))

    def publish(self, req: PublishRequest) -> PublishResult:
        responses: list = []
        if self.quota_left(responses) <= 0:
            raise QuotaFull("Instagram 24-hour publishing limit reached")
        caption = " ".join(x for x in [req.title, req.description, " ".join(req.hashtags)] if x).strip()[:2200]
        path = req.path
        if self.max_seconds and self.trim and (req.duration_s or 0) > self.max_seconds:
            path = self.trim(path, path.with_name(path.stem + f"_{int(self.max_seconds)}s.mp4"), self.max_seconds)
            responses.append({"path": "trimmed", "from_s": req.duration_s, "to_s": self.max_seconds})
        try:
            cid = self._upload_retrying(path, caption, responses)
        except PublishError as e:
            if "ProcessingFailedError" not in str(e) or not self.reencode:
                raise
            # Still refused after retries: re-encode it strictly and try again.
            safe = self.reencode(path, path.with_name(path.stem + "_ig.mp4"))
            responses.append({"path": "reencoded", "file": str(safe)})
            cid = self._upload_retrying(safe, caption, responses)
        return self._finish(cid, responses)

    def _upload_retrying(self, path, caption: str, responses: list) -> str:
        """rupload answers ProcessingFailedError at random (seen 14 Nov 2026: the same file was refused,
        then accepted on the next try), so each file gets several attempts with a fresh container."""
        for attempt in range(self.upload_attempts):
            try:
                return self._upload(path, caption, responses)
            except PublishError as e:
                if "ProcessingFailedError" not in str(e) or attempt == self.upload_attempts - 1:
                    raise
                responses.append({"path": "rupload_retry", "attempt": attempt + 1})
                self.sleep(min(60, 10 * 2 ** attempt))
        raise AssertionError("unreachable")

    def _upload(self, path, caption: str, responses: list) -> str:
        container = self._call("POST", f"/{self.user_id}/media", responses, data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption, "share_to_feed": "true",
            "access_token": self.token})
        cid, uri = container.get("id"), container.get("uri")
        if not cid or not uri:
            raise PublishError("Instagram did not return an upload URI")
        size = path.stat().st_size
        try:
            with open(path, "rb") as f:
                up = self.client.post(uri, content=f.read(), headers={
                    "Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(size)})
            responses.append({"path": "rupload", "status_code": up.status_code})
        except httpx.HTTPError as e:
            raise PublishError(f"Instagram upload failed ({type(e).__name__})", retryable=True) from None
        if up.status_code != 200:
            kind = ""
            try:
                kind = (up.json().get("debug_info") or {}).get("type", "")
            except ValueError:
                pass
            raise PublishError(f"Instagram upload HTTP {up.status_code} {kind}".strip(),
                               retryable=up.status_code >= 500)
        return cid

    def _finish(self, cid: str, responses: list) -> PublishResult:

        for _ in range(self.max_polls):
            st = self._call("GET", f"/{cid}", responses,
                            params={"fields": "status_code,status", "access_token": self.token})
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram processing {code}: {st.get('status', '')}")
            self.sleep(self.poll_seconds)
        else:
            raise PublishError("Instagram processing timed out", retryable=True)

        media = self._call("POST", f"/{self.user_id}/media_publish", responses,
                           data={"creation_id": cid, "access_token": self.token})
        mid = media.get("id")
        link = self._call("GET", f"/{mid}", responses, params={"fields": "permalink", "access_token": self.token})
        return PublishResult(str(mid), link.get("permalink", ""), "posted", "public", responses)
