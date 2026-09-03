"""Fetches the infra node's sacct dumps.

`GET /api/download/jobs-raw-<cloud>` answers 302 with an S3 presigned URL and
the file is downloaded directly from S3, so a two-core serving box can hand
out a 191MB export. Three consequences shape this module:

  * **The redirect must be followed**, and by us rather than by a caller that
    might log the destination.
  * **The presigned URL is a credential.** Its signature is valid for 900
    seconds. It is never returned, never logged, never written to a manifest,
    and never cached for a later request; a second download asks the infra
    node again.
  * **The request is a plain GET and needs no authentication.** Reaching the
    tailnet is the access control. Nothing here reads or sends a credential,
    and if the endpoint ever starts asking for one, that is a decision to
    bring back rather than a scheme to guess at.

Only `http://` to the configured infra host is allowed for the first hop, and
only `https://` for the redirect. A redirect anywhere else is refused instead
of followed.
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = os.environ.get("SLURM_DUMP_BASE_URL") or "http://infra-node:8888"
DOWNLOAD_PATH = "/api/download/jobs-raw-{cloud}"
CHUNK_BYTES = 1024 * 1024


class SlurmDumpError(RuntimeError):
    """The dump could not be fetched. Never carries a presigned URL."""


class SlurmDumpFetcher:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 900,
        opener: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener(_StrictRedirectHandler())

    def fetch(self, cloud: str, destination: Path) -> dict[str, Any]:
        """Download one cloud's dump. Returns facts about the file, not its URL."""
        if not cloud or "/" in cloud or ".." in cloud:
            raise SlurmDumpError(f"refusing suspicious cloud name: {cloud!r}")
        url = self.base_url + DOWNLOAD_PATH.format(cloud=urllib.parse.quote(cloud))
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        written = 0
        request = urllib.request.Request(url, method="GET", headers={"Accept": "*/*"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                if status != 200:
                    raise SlurmDumpError(f"{cloud}: unexpected status {status}")
                with open(destination, "wb") as handle:
                    while True:
                        chunk = response.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        handle.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
        except urllib.error.HTTPError as error:
            # Only the status is reported: an error body from S3 can echo the
            # signed query string back at us.
            raise SlurmDumpError(f"{cloud}: HTTP {error.code}") from None
        except urllib.error.URLError as error:
            raise SlurmDumpError(f"{cloud}: unreachable ({error.reason})") from None
        if not written:
            raise SlurmDumpError(f"{cloud}: empty download")
        return {
            "dump_bytes": written,
            "dump_sha256": digest.hexdigest(),
            # Deliberately not the presigned URL: the source is named by the
            # endpoint that issued the redirect, which is stable and safe.
            "dump_source": self.base_url + DOWNLOAD_PATH.format(cloud=cloud),
        }


class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows the 302 to S3 over https, and refuses anything else."""

    def redirect_request(self, request, fp, code, message, headers, newurl):  # type: ignore[override]
        scheme = urllib.parse.urlsplit(newurl).scheme
        if scheme != "https":
            raise SlurmDumpError(f"refusing redirect to a {scheme or 'schemeless'} location")
        return super().redirect_request(request, fp, code, message, headers, newurl)
