"""A minimal Kubernetes API client.

Plain HTTPS with a bearer token. Heimdall uses two of these, built from two
different ServiceAccount tokens: a read-only one for the investigator and an
actor one for the executor. They share no state.
"""

from pathlib import Path
from typing import Any

import httpx

IN_CLUSTER_URL = "https://kubernetes.default.svc"
IN_CLUSTER_CA = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


class KubeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class KubeClient:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    @classmethod
    def connect(
        cls, server: str, token: str, ca: Path | str | bool = True, timeout: float = 15
    ) -> "KubeClient":
        verify: Any = str(ca) if isinstance(ca, Path) else ca
        return cls(
            httpx.Client(
                base_url=server,
                headers={"Authorization": f"Bearer {token}"},
                verify=verify,
                timeout=timeout,
            )
        )

    def _check(self, response: httpx.Response) -> httpx.Response:
        if response.is_success:
            return response
        try:
            message = response.json().get("message", response.text)
        except ValueError:
            message = response.text
        raise KubeError(response.status_code, message)

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        result: dict[str, Any] = self._check(self._client.get(path, params=params)).json()
        return result

    def get_text(self, path: str, **params: Any) -> str:
        params = {k: v for k, v in params.items() if v is not None}
        return self._check(self._client.get(path, params=params)).text
