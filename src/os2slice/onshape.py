"""Thin read-only Onshape REST client. Request shapes are verified in docs/ONSHAPE_API.md."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from os2slice import __version__
from os2slice.auth import Keys
from os2slice.errors import AuthError, BadRequest, NoAccess, OnshapeError
from os2slice.oauth import SIGN_IN_AGAIN, USER_ID_RE
from os2slice.request import ONSHAPE_HOST_RE, ExportRequest

log = logging.getLogger(__name__)

JSON = "application/json"
REDIRECTS = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 3


def is_onshape_url(url: httpx.URL) -> bool:
    return url.scheme == "https" and bool(ONSHAPE_HOST_RE.fullmatch(url.host)) and url.port is None


class OnshapeClient:
    def __init__(
        self,
        base_url: str,
        keys: Keys | httpx.Auth,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        """`keys`: the shared API key pair, or a signed-in user's bearer auth (D-23)."""
        self._signed_in = isinstance(keys, httpx.Auth)
        self._client = httpx.Client(
            base_url=base_url,
            auth=keys if isinstance(keys, httpx.Auth) else (keys.access, keys.secret),
            timeout=timeout,
            # Redirects are followed by hand so auth only ever goes to *.onshape.com.
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": f"os2slice/{__version__}"},
        )

    def __enter__(self) -> OnshapeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- API calls ---------------------------------------------------------

    def check_keys(self) -> str:
        """Return the account's email ("" if hidden). Onshape answers bad keys with 204, not 401."""
        r = self._get("/api/users/sessioninfo", accept=JSON)
        if r.status_code != 200:
            raise AuthError(
                "Onshape didn't accept the API keys", "Run `os2slice setup-keys` with a valid pair"
            )
        email = self._json(r).get("email")
        return email if isinstance(email, str) else ""

    def whoami(self) -> tuple[str, str]:
        """(user id, display name) of whoever the client is signed in as."""
        r = self._get("/api/users/sessioninfo", accept=JSON)
        body = self._json(r) if r.status_code == 200 else {}
        uid = body.get("id")
        if not isinstance(uid, str) or not USER_ID_RE.fullmatch(uid):
            raise AuthError("Onshape didn't say who signed in", SIGN_IN_AGAIN)
        name = body.get("name") or body.get("email")
        return uid, name if isinstance(name, str) and name else "Onshape user"

    def get_document_name(self, document_id: str) -> str:
        r = self._get(f"/api/documents/{document_id}", accept=JSON)
        name = self._json(r).get("name")
        return name if isinstance(name, str) and name else document_id

    def list_parts(self, req: ExportRequest) -> list[dict[str, Any]]:
        r = self._get(
            f"/api/parts/d/{req.document_id}/{req.wvm}/{req.wvm_id}/e/{req.element_id}",
            params=self._config_params(req),
            accept=JSON,
        )
        parts = self._json(r)
        if not isinstance(parts, list):
            raise OnshapeError("Unexpected part list from Onshape")
        return [p for p in parts if isinstance(p, dict)]

    def get_part_name(self, req: ExportRequest) -> str:
        for p in self.list_parts(req):
            if p.get("partId") == req.part_id and isinstance(p.get("name"), str):
                return p["name"]
        log.warning("part %s not in the part list; using its ID as the name", req.part_id)
        return req.part_id or "part"

    def part_of_face(self, req: ExportRequest, face_id: str) -> str:
        """The part (body) ID a face belongs to, for a face picked without its part."""
        r = self._get(
            f"{self._element_path(req)}/bodydetails", params=self._config_params(req), accept=JSON
        )
        bodies = self._json(r).get("bodies", [])
        for body in bodies if isinstance(bodies, list) else []:
            if any(f.get("id") == face_id for f in body.get("faces", [])):
                return str(body["id"])
        raise BadRequest(f"Face {face_id!r} isn't on any part in this Part Studio")

    def face_normal(self, req: ExportRequest, face_id: str) -> tuple[float, float, float]:
        """Outward unit normal of a planar face of the requested part (docs/ONSHAPE_API.md)."""
        r = self._get(
            f"{self._element_path(req)}/bodydetails",
            params=self._config_params(req),
            accept=JSON,
        )
        bodies = self._json(r).get("bodies", [])
        for body in bodies if isinstance(bodies, list) else []:
            if body.get("id") != req.part_id:
                continue
            for face in body.get("faces", []):
                if face.get("id") != face_id:
                    continue
                surface = face.get("surface") or {}
                if surface.get("type") != "plane":
                    raise BadRequest(
                        "The selected face isn't flat", "Pick a planar face to put on the bed"
                    )
                n = surface.get("normal")
                if not (isinstance(n, list) and len(n) == 3):
                    raise OnshapeError("Onshape returned a plane without a normal")
                sign = 1.0 if face.get("orientation", True) else -1.0
                return (sign * float(n[0]), sign * float(n[1]), sign * float(n[2]))
            raise BadRequest(f"Face {face_id!r} isn't on part {req.part_id!r}")
        raise OnshapeError(f"Part {req.part_id!r} not found in the Part Studio")

    def export_stl(self, req: ExportRequest, units: str = "millimeter") -> bytes:
        if req.part_id is None:
            raise OnshapeError("Whole Part Studio export isn't supported yet")
        params = {
            "partIds": req.part_id,
            "mode": "binary",
            "units": units,
            "grouping": "true",
            **self._config_params(req),
        }
        r = self._get(
            f"{self._element_path(req)}/stl",
            params=params,
            accept="application/vnd.onshape.v1+octet-stream",
        )
        data = r.content
        check_binary_stl(data)
        return data

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _element_path(req: ExportRequest) -> str:
        return f"/api/partstudios/d/{req.document_id}/{req.wvm}/{req.wvm_id}/e/{req.element_id}"

    @staticmethod
    def _config_params(req: ExportRequest) -> dict[str, str]:
        return {"configuration": req.configuration} if req.configuration else {}

    def _get(
        self, path: str, params: dict[str, str] | None = None, accept: str = JSON
    ) -> httpx.Response:
        url: str | httpx.URL = path
        for _ in range(MAX_REDIRECTS + 1):
            try:
                r = self._client.get(url, params=params, headers={"Accept": accept})
            except httpx.TimeoutException as e:
                raise OnshapeError("Onshape didn't answer in time", "Try again") from e
            except httpx.TransportError as e:
                raise OnshapeError(
                    f"Can't reach Onshape ({type(e).__name__})", "Check your network connection"
                ) from e
            log.debug("GET %s -> %s", r.request.url.copy_with(query=None), r.status_code)
            if r.status_code not in REDIRECTS:
                if r.status_code == 401 and self._signed_in:
                    raise AuthError("Onshape no longer accepts your sign-in", SIGN_IN_AGAIN)
                self._raise_for_status(r)
                return r
            target = r.request.url.join(r.headers.get("location", ""))
            if not is_onshape_url(target):
                raise OnshapeError(f"Refusing to follow a redirect to {target.host!r}")
            url, params = target, None  # the Location already carries the query
        raise OnshapeError("Too many redirects from Onshape")

    @staticmethod
    def _raise_for_status(r: httpx.Response) -> None:
        if r.is_success:
            return
        status = r.status_code
        detail = ""
        try:
            body = r.json()
            if isinstance(body, dict) and isinstance(body.get("message"), str):
                detail = f": {body['message'][:200]}"
        except ValueError:
            pass
        if status == 401:
            raise AuthError(
                "Onshape rejected the API keys (401)", "Run `os2slice setup-keys` with a valid pair"
            )
        if status == 403:
            raise NoAccess(
                "No access to this document (403)",
                "Check the API keys' scope and that your account can open the document",
            )
        if status == 404:
            raise OnshapeError(f"Onshape couldn't find the document, element or part (404){detail}")
        if status == 429:
            raise OnshapeError("Onshape rate limit hit (429)", "Wait a minute and try again")
        raise OnshapeError(f"Onshape API error {status}{detail}")

    @staticmethod
    def _json(r: httpx.Response) -> Any:
        try:
            return r.json()
        except ValueError as e:
            raise OnshapeError("Onshape returned invalid JSON") from e


def check_binary_stl(data: bytes) -> int:
    """Return the triangle count, or raise if `data` isn't a non-empty binary STL."""
    if len(data) < 84:
        raise OnshapeError("Onshape returned a file too small to be an STL")
    count = int.from_bytes(data[80:84], "little")
    if len(data) != 84 + 50 * count:
        raise OnshapeError("Onshape returned something that isn't a binary STL")
    if count == 0:
        raise OnshapeError("The exported mesh is empty", "Check that the part has solid geometry")
    return count
