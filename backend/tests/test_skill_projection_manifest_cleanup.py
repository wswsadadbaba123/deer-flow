"""Projection manifest failures must release resources and retain their cause."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from deerflow.skills import projection


@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
@pytest.mark.parametrize("stage", ["write", "replace"])
@pytest.mark.parametrize("interrupted", [False, True], ids=["io-error", "interrupt"])
@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["cleanup-ok", "cleanup-error"])
def test_failed_manifest_preserves_error_and_attempts_cleanup(tmp_path, monkeypatch, existing, stage, interrupted, cleanup_fails, caplog):
    target = tmp_path / ".projection-manifest.json"
    if existing:
        target.write_bytes(b"previous manifest")
    failure = KeyboardInterrupt("publication interrupted") if interrupted else OSError("publication failed")
    unlink = Path.unlink
    attempts = []

    def fail(*args, **kwargs):
        if stage == "write":
            args[1].write('{"partial":')
        raise failure

    def cleanup(path, **kwargs):
        attempts.append(path)
        if cleanup_fails:
            raise PermissionError("cleanup failed")
        return unlink(path, **kwargs)

    with monkeypatch.context() as patches:
        patches.setattr(json if stage == "write" else Path, "dump" if stage == "write" else "replace", fail)
        patches.setattr(Path, "unlink", cleanup)
        with pytest.raises(type(failure)) as caught:
            projection._write_manifest(tmp_path, "source", "view")

    assert caught.value is failure
    assert len(attempts) == 1
    assert attempts[0].parent == tmp_path
    assert attempts[0].name.startswith(".projection-manifest-")
    assert attempts[0].exists() is cleanup_fails
    records = [record for record in caplog.records if record.name == projection.__name__]
    assert len(records) == int(cleanup_fails)
    if cleanup_fails:
        assert records[0].levelno == logging.WARNING
        assert str(attempts[0]) in records[0].getMessage()
        assert isinstance(records[0].exc_info[1], PermissionError)
    if existing:
        assert target.read_bytes() == b"previous manifest"
    else:
        assert not target.exists()


@pytest.mark.parametrize("close_fails", [False, True], ids=["close-ok", "close-error"])
def test_fdopen_failure_closes_descriptor_before_cleanup(tmp_path, monkeypatch, close_fails, caplog):
    failure = OSError("fdopen failed")
    close = os.close
    unlink = Path.unlink
    descriptors = []
    cleanup_attempts = []

    def fail_fdopen(fd, *args, **kwargs):
        descriptors.append(fd)
        raise failure

    def close_descriptor(fd):
        close(fd)
        if close_fails:
            raise OSError("close failed")

    def cleanup(path, **kwargs):
        cleanup_attempts.append(path)
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
        return unlink(path, **kwargs)

    try:
        with monkeypatch.context() as patches:
            patches.setattr(os, "fdopen", fail_fdopen)
            patches.setattr(os, "close", close_descriptor)
            patches.setattr(Path, "unlink", cleanup)
            with pytest.raises(OSError) as caught:
                projection._write_manifest(tmp_path, "source")
        assert caught.value is failure
        assert len(cleanup_attempts) == 1
        assert list(tmp_path.iterdir()) == []
        records = [record for record in caplog.records if record.name == projection.__name__]
        assert len(records) == int(close_fails)
        if close_fails:
            assert records[0].levelno == logging.WARNING
            assert "close" in records[0].getMessage()
            assert records[0].exc_info[1].args == ("close failed",)
    finally:
        # Keep the pre-fix regression run from leaking its deliberately captured fd.
        for fd in descriptors:
            try:
                os.fstat(fd)
            except OSError:
                continue
            close(fd)


def test_successful_manifest_closes_descriptor_and_keeps_schema(tmp_path, monkeypatch):
    fdopen = os.fdopen
    descriptors = []

    def open_stream(fd, *args, **kwargs):
        descriptors.append(fd)
        return fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", open_stream)
    projection._write_manifest(tmp_path, "source", "view")

    assert json.loads((tmp_path / ".projection-manifest.json").read_text(encoding="utf-8")) == {
        "version": projection._MANIFEST_VERSION,
        "source_signature": "source",
        "view_signature": "view",
    }
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert list(tmp_path.glob("*.tmp")) == []
