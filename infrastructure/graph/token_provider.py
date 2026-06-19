# infrastructure/graph/token_provider.py
from __future__ import annotations

import base64
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class GraphTokenTransientError(RuntimeError):
    """Error transitorio al obtener token de Microsoft Graph."""


@dataclass(frozen=True)
class GraphCreds:
    tenant_id: str
    client_id: str
    client_secret: str


def _try_json(value: str) -> Optional[dict]:
    try:
        return json.loads(value)
    except Exception:
        return None


def _try_b64_json(value: str) -> Optional[dict]:
    try:
        raw = base64.b64decode(value).decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


def parse_graph_key(graph_key: str) -> Tuple[Optional[GraphCreds], Optional[str]]:
    data = _try_json(graph_key) or _try_b64_json(graph_key)
    if isinstance(data, dict) and all(
        key in data for key in ("tenant_id", "client_id", "client_secret")
    ):
        return (
            GraphCreds(
                tenant_id=str(data["tenant_id"]),
                client_id=str(data["client_id"]),
                client_secret=str(data["client_secret"]),
            ),
            None,
        )
    return None, graph_key


class GraphTokenProvider:
    def __init__(self, graph_key: str, timeout_s: int) -> None:
        self._creds, self._raw_token = parse_graph_key(graph_key)
        self._timeout_s = int(timeout_s)
        self._cached_token: Optional[str] = None
        self._cached_exp: float = 0.0
        self._max_attempts = 5
        self._backoff_base_s = 0.8
        self._backoff_cap_s = 20.0
        timeout = httpx.Timeout(
            self._timeout_s,
            connect=min(30, self._timeout_s),
        )
        limits = httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=30.0,
        )
        self._client = httpx.Client(
            timeout=timeout,
            limits=limits,
            trust_env=True,
        )

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except Exception:
            return None

    def _sleep_backoff(
        self,
        attempt: int,
        *,
        retry_after: Optional[float] = None,
    ) -> None:
        base = self._backoff_base_s * (2 ** (attempt - 1))
        jitter = random.random() * (0.25 * base)
        sleep_s = min(base + jitter, self._backoff_cap_s)
        if retry_after is not None:
            sleep_s = max(sleep_s, min(retry_after, self._backoff_cap_s))
        time.sleep(sleep_s)

    def get_token(self) -> str:
        if self._raw_token:
            return self._raw_token

        if not self._creds:
            raise RuntimeError(
                "GRAPH_KEY inválido. Debe ser JSON o base64 JSON con "
                "tenant_id, client_id y client_secret."
            )

        now = time.time()
        if self._cached_token and now < (self._cached_exp - 60):
            return self._cached_token

        return self._fetch_token_with_retry()

    def _fetch_token_with_retry(self) -> str:
        assert self._creds is not None
        url = (
            "https://login.microsoftonline.com/"
            f"{self._creds.tenant_id}/oauth2/v2.0/token"
        )
        data = {
            "client_id": self._creds.client_id,
            "client_secret": self._creds.client_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        }

        last_exc: Optional[Exception] = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.post(url, data=data)
                if response.status_code == 200:
                    payload = response.json()
                    token = payload["access_token"]
                    expires_in = int(payload.get("expires_in", 3599))
                    now = time.time()
                    self._cached_token = token
                    self._cached_exp = now + expires_in
                    return token

                if response.status_code in _RETRYABLE_STATUS_CODES:
                    retry_after = self._retry_after_seconds(response)
                    logger.warning(
                        "AzureAD token retryable. attempt=%s/%s status=%s "
                        "retry_after=%s",
                        attempt,
                        self._max_attempts,
                        response.status_code,
                        retry_after,
                    )
                    if attempt < self._max_attempts:
                        self._sleep_backoff(
                            attempt,
                            retry_after=retry_after,
                        )
                        continue
                    raise GraphTokenTransientError(
                        f"Token endpoint {response.status_code}: "
                        f"{response.text[:300]}"
                    )

                raise RuntimeError(
                    "Token error no reintentable "
                    f"{response.status_code}: {response.text[:500]}"
                )
            except (
                httpx.ConnectTimeout,
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                last_exc = exc
                logger.warning(
                    "AzureAD token fallo red/TLS. attempt=%s/%s err=%r",
                    attempt,
                    self._max_attempts,
                    exc,
                )
                if attempt < self._max_attempts:
                    self._sleep_backoff(attempt)
                    continue
                raise GraphTokenTransientError(
                    f"Token request failed after retries: {exc!r}"
                ) from exc

        raise GraphTokenTransientError(f"Token request failed: {last_exc!r}")
