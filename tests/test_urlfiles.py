import functools
import hashlib
import http.server
import shutil
import subprocess
import threading

import httpx
import pytest
import respx

from dataops_mcp import transfer, urlfiles
from dataops_mcp.util import read_tsv


def test_make_row_sanitises_and_normalises():
    row = urlfiles.make_row("https://example.org/a/b/My%20File.txt.gz", subdir="../x/./y", size="12",
                            checksum="ABCDEF")
    assert row["filename"] == "My_File.txt.gz" and row["subdir"] == "x/y"
    assert row["size"] == 12 and row["checksum"] == "md5:abcdef"
    assert urlfiles.normalize_checksum("sha256:FF") == "sha256:ff"
    assert urlfiles.normalize_checksum("crc32:1234") == ""
    with pytest.raises(ValueError):
        urlfiles.make_row("ftp://example.org/x")


@respx.mock
def test_inspect_urls_records_sizes(cfg, db, client):
    respx.head("https://example.org/data/a.vcf.gz").mock(
        return_value=httpx.Response(200, headers={"content-length": "1234"}))
    respx.head("https://example.org/data/missing.txt").mock(return_value=httpx.Response(404))
    out = urlfiles.inspect_urls(cfg, db, client, ["https://example.org/data/a.vcf.gz",
                                                  "https://example.org/data/missing.txt"])
    assert out["summary"]["n_files"] == 1 and out["summary"]["total_bytes"] == 1234
    assert out["unreachable"][0]["url"].endswith("missing.txt")
    assert read_tsv(out["manifest_path"])[0]["filename"] == "a.vcf.gz"


@pytest.fixture
def local_server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl 필요")
def test_url_download_script_runs_end_to_end(cfg, db, local_server, monkeypatch):
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    root, base = local_server
    (root / "good.txt").write_bytes(b"hello")
    (root / "bad.txt").write_bytes(b"world")
    rows = [
        urlfiles.make_row(f"{base}/good.txt", subdir="suppl", size=5,
                          checksum="md5:" + hashlib.md5(b"hello").hexdigest()),
        urlfiles.make_row(f"{base}/bad.txt", size=5, checksum="sha256:" + "0" * 64),
    ]
    q = urlfiles.save_url_query(cfg, db, "zenodo", rows, {"record": "1"}, [], {})
    out = transfer.prepare_download(cfg, db, q["query_id"])
    script = out["script_path"]
    assert "fetch " in out["script_text"] and "curl --fail --location" in out["script_text"]

    first = subprocess.run(["bash", script], capture_output=True, text=True)
    dest = transfer.default_dest(cfg, db.get_query(q["query_id"]))
    assert (dest / "suppl" / "good.txt").read_bytes() == b"hello"
    assert first.returncode == 1 and "CHECKSUM MISMATCH] bad.txt" in first.stdout

    rep = transfer.verify_download(cfg, db, q["query_id"], check_md5=True)
    assert rep["status"] == "FAIL" and rep["md5_mismatch"] == ["bad.txt"]

    second = subprocess.run(["bash", script], capture_output=True, text=True)
    assert "[skip] suppl/good.txt" in second.stdout  # 이미 받은 파일은 건너뜀


def test_url_source_goes_through_review(cfg, db):
    from dataops_mcp import review

    rows = [urlfiles.make_row("https://example.org/a.h5ad", size=10, checksum=None)]
    q = urlfiles.save_url_query(cfg, db, "cellxgene", rows, {}, [], {})
    out = transfer.prepare_download(cfg, db, q["query_id"])
    assert any("체크섬" in w for w in out["warnings"])
    res = review.preflight_check(cfg, db, out["script_path"])
    assert res["verdict"] == "PASS", res["defects"]
