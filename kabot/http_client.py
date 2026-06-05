from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class HttpClient:
    def __init__(self, timeout: int = 30, user_agent: str = "kabot/0.1") -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> HttpResponse:
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            url = f"{url}?{query}" if "?" not in url else f"{url}&{query}"
        data = None
        req_headers = {"User-Agent": self.user_agent, **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return HttpResponse(resp.status, dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise HttpError(f"HTTP {exc.code} for {url}", status=exc.code, body=body) from exc
        except urllib.error.URLError as exc:
            raise HttpError(f"Request failed for {url}: {exc.reason}") from exc
