"""Open a ``StreamSource`` URL for byte-forwarding — remote (``requests``) or a
local file (``file://``, from the local-library provider).

The web stream proxy and the cast relay both forward a provider stream's bytes
with ``Range`` passthrough. Provider streams are HTTP(S); a library track is a
file on this machine. :func:`open_stream` hides the difference: for ``file://``
it returns a small response object with the subset of the ``requests.Response``
surface those callers use (``status_code``, ``headers``, ``iter_content``,
``close`` and the context-manager protocol), honouring a single byte range.
Engine-layer, gi-free.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

_CHUNK = 64 * 1024
_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$")


def is_file_url(url: str) -> bool:
    return (url or "").lower().startswith("file:")


def file_url_path(url: str) -> Path:
    """The filesystem path a ``file://`` URL names."""
    return Path(unquote(urlsplit(url).path))


class LocalFileResponse:
    """A ``requests.Response``-shaped view of (a byte range of) a local file."""

    def __init__(self, path: Path, mime: str | None, range_header: str | None) -> None:
        self.path = path
        size = path.stat().st_size
        self.headers: dict[str, str] = {"Accept-Ranges": "bytes"}
        if mime:
            self.headers["Content-Type"] = mime
        start, end = 0, size - 1
        self.status_code = 200
        m = _RANGE_RE.match(range_header or "") if range_header else None
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:  # suffix range: the last N bytes
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                self.status_code = 416
                self.headers["Content-Range"] = f"bytes */{size}"
                self.headers["Content-Length"] = "0"
                self._remaining = 0
                self._fh = None
                return
            self.status_code = 206
            self.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._remaining = max(0, end - start + 1)
        self.headers["Content-Length"] = str(self._remaining)
        self._fh: Any = open(path, "rb")  # noqa: SIM115 - closed by close()/__exit__
        self._fh.seek(start)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def iter_content(self, chunk_size: int = _CHUNK) -> Iterator[bytes]:
        while self._remaining > 0 and self._fh is not None:
            data = self._fh.read(min(chunk_size or _CHUNK, self._remaining))
            if not data:
                return
            self._remaining -= len(data)
            yield data

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> LocalFileResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_stream(url: str, headers: dict[str, str] | None = None, *, mime: str | None = None,
                timeout: Any = 20) -> Any:
    """GET ``url`` as a streaming response; ``file://`` is served from disk.

    Raises ``OSError`` for a missing/unreadable local file and
    ``requests.RequestException`` for a failed remote fetch, so callers keep a
    single error path.
    """
    if is_file_url(url):
        path = file_url_path(url)
        if not path.is_file() or not os.access(path, os.R_OK):
            raise FileNotFoundError(f"library file is missing: {path}")
        return LocalFileResponse(path, mime, (headers or {}).get("Range"))
    import requests

    return requests.get(url, headers=headers or {}, stream=True, timeout=timeout,
                        allow_redirects=True)


__all__ = ["LocalFileResponse", "file_url_path", "is_file_url", "open_stream"]
