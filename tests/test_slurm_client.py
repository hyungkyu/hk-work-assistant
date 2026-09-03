"""The Slurm dump fetcher: redirect discipline and credential hygiene.

A fake opener stands in for urllib, so no request leaves the machine. The
presigned URL is the thing under test: it is a 900-second credential, and the
fetcher must never hand it back, log it, or let it reach a manifest.
"""

from __future__ import annotations

import gzip
import io
import urllib.error
from pathlib import Path

import pytest

from rlwrld_worklog.slurm_client import DOWNLOAD_PATH, SlurmDumpError, SlurmDumpFetcher

SIGNED = "https://s3.invalid/dump.psv.gz?X-Amz-Signature=deadbeefnotreal"


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requested: list[str] = []

    def open(self, request, timeout: int | None = None):
        self.requested.append(request.full_url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _fetcher(response: object) -> tuple[SlurmDumpFetcher, FakeOpener]:
    opener = FakeOpener(response)
    return SlurmDumpFetcher(base_url="http://infra-node.invalid:8888", opener=opener), opener


class TestFetch:
    def test_the_file_is_written_and_described_by_size_and_digest(self, tmp_path: Path) -> None:
        body = gzip.compress(b"Cluster|JobID|State|End\n")
        fetcher, opener = _fetcher(FakeResponse(body))

        facts = fetcher.fetch("kakao", tmp_path / "kakao.psv.gz")

        assert (tmp_path / "kakao.psv.gz").read_bytes() == body
        assert facts["dump_bytes"] == len(body)
        assert len(facts["dump_sha256"]) == 64
        assert opener.requested == ["http://infra-node.invalid:8888" + DOWNLOAD_PATH.format(cloud="kakao")]

    def test_the_reported_source_is_the_endpoint_never_the_signed_url(self, tmp_path: Path) -> None:
        fetcher, _ = _fetcher(FakeResponse(gzip.compress(b"a|b\n")))

        facts = fetcher.fetch("naver", tmp_path / "naver.psv.gz")

        assert facts["dump_source"].endswith("/api/download/jobs-raw-naver")
        assert "X-Amz-Signature" not in " ".join(str(value) for value in facts.values())
        assert not any(key in facts for key in ("url", "presigned_url", "location"))

    def test_an_empty_download_is_an_error_not_an_empty_dump(self, tmp_path: Path) -> None:
        fetcher, _ = _fetcher(FakeResponse(b""))

        with pytest.raises(SlurmDumpError):
            fetcher.fetch("kakao", tmp_path / "kakao.psv.gz")

    def test_an_unexpected_status_is_refused(self, tmp_path: Path) -> None:
        fetcher, _ = _fetcher(FakeResponse(b"body", status=204))

        with pytest.raises(SlurmDumpError):
            fetcher.fetch("kakao", tmp_path / "kakao.psv.gz")

    def test_an_http_error_reports_only_its_status(self, tmp_path: Path) -> None:
        error = urllib.error.HTTPError(SIGNED, 403, "Forbidden", {}, None)
        fetcher, _ = _fetcher(error)

        with pytest.raises(SlurmDumpError) as caught:
            fetcher.fetch("kakao", tmp_path / "kakao.psv.gz")

        # An S3 error body, and its URL, can echo the signature back at us.
        assert "403" in str(caught.value)
        assert "X-Amz-Signature" not in str(caught.value)
        assert "s3.invalid" not in str(caught.value)

    def test_an_unreachable_host_is_reported_without_a_traceback_chain(self, tmp_path: Path) -> None:
        fetcher, _ = _fetcher(urllib.error.URLError("Name or service not known"))

        with pytest.raises(SlurmDumpError) as caught:
            fetcher.fetch("kakao", tmp_path / "kakao.psv.gz")

        assert "unreachable" in str(caught.value)
        assert caught.value.__cause__ is None

    def test_a_suspicious_cloud_name_never_reaches_a_url(self, tmp_path: Path) -> None:
        fetcher, opener = _fetcher(FakeResponse(gzip.compress(b"a|b\n")))

        for cloud in ("", "../secrets", "a/b"):
            with pytest.raises(SlurmDumpError):
                fetcher.fetch(cloud, tmp_path / "out.psv.gz")
        assert opener.requested == []


class TestRedirectDiscipline:
    def test_only_https_redirects_are_followed(self) -> None:
        from rlwrld_worklog.slurm_client import _StrictRedirectHandler

        handler = _StrictRedirectHandler()
        with pytest.raises(SlurmDumpError):
            handler.redirect_request(None, None, 302, "Found", {}, "http://elsewhere.invalid/x")
        with pytest.raises(SlurmDumpError):
            handler.redirect_request(None, None, 302, "Found", {}, "/relative/path")
