"""YouTube Shorts: resumable videos.insert from the local file.

Projects that have not passed YouTube's API compliance audit get every upload forced to private.
The publisher reports that as status 'locked_private' so the caller can hold YouTube posts and
alert the user, instead of silently filling the channel with private videos.
"""
import json
import time
from typing import Callable

import httpx

from autoclip.publishers import PublishError, PublishRequest, PublishResult

UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTubePublisher:
    platform = "youtube"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 client: httpx.Client | None = None, sleep: Callable = time.sleep, retries: int = 3):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, write=600, read=600))
        self.sleep, self.retries = sleep, retries

    def _token(self) -> str:
        try:
            r = self.client.post("https://oauth2.googleapis.com/token", data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise PublishError(f"YouTube token request failed ({type(e).__name__})", retryable=True) from None
        if r.status_code != 200 or "access_token" not in body:
            raise PublishError(f"YouTube token rejected: HTTP {r.status_code} {body.get('error', '')}")
        return body["access_token"]

    def publish(self, req: PublishRequest) -> PublishResult:
        token = self._token()
        meta = {
            "snippet": {"title": req.title[:100], "description": (req.description + "\n\n" + " ".join(req.hashtags))
                        .strip()[:5000], "tags": [h.lstrip("#") for h in req.hashtags][:15], "categoryId": "24"},
            "status": {"privacyStatus": req.privacy, "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": False},
        }
        size = req.path.stat().st_size
        responses, last = [], None
        for attempt in range(1, self.retries + 1):
            try:
                init = self.client.post(
                    UPLOAD, params={"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "false"},
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                             "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
                    content=json.dumps(meta))
                if init.status_code != 200 or "location" not in init.headers:
                    raise PublishError(f"YouTube upload init HTTP {init.status_code}: {init.text[:200]}",
                                       retryable=init.status_code >= 500)
                with open(req.path, "rb") as f:
                    up = self.client.put(init.headers["location"], content=f.read(),
                                         headers={"Content-Type": "video/mp4", "Authorization": f"Bearer {token}"})
                body = up.json()
                responses.append({"status_code": up.status_code, "body": body})
                if up.status_code not in (200, 201):
                    reason = ((body.get("error") or {}).get("errors") or [{}])[0].get("reason", "")
                    raise PublishError(f"YouTube upload HTTP {up.status_code} {reason}",
                                       retryable=up.status_code >= 500)
                vid = body["id"]
                got = body.get("status", {}).get("privacyStatus", req.privacy)
                locked = req.privacy != "private" and got == "private"
                return PublishResult(vid, f"https://www.youtube.com/shorts/{vid}",
                                     "locked_private" if locked else "posted", got, responses)
            except (httpx.HTTPError, ValueError) as e:
                last = PublishError(f"YouTube upload failed ({type(e).__name__})", retryable=True)
            except PublishError as e:
                last = e
                if not e.retryable:
                    break
            if attempt < self.retries:
                self.sleep(2 ** attempt * 5)
        raise last

    def set_privacy(self, video_id: str, privacy: str) -> None:
        """Used once the audit passes, to make held private uploads public."""
        token = self._token()
        r = self.client.put("https://www.googleapis.com/youtube/v3/videos", params={"part": "status"},
                            headers={"Authorization": f"Bearer {token}"},
                            json={"id": video_id, "status": {"privacyStatus": privacy}})
        if r.status_code != 200:
            raise PublishError(f"YouTube privacy update HTTP {r.status_code}")
