from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_dataops_agent.team import ComfortDownloadTeam


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class ComfortDownloadTeamTests(unittest.TestCase):
    def test_download_data_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "data.txt"
            with patch(
                "personal_dataops_agent.team.urlopen",
                return_value=_FakeResponse(b"sample-data"),
            ):
                result = ComfortDownloadTeam().download_data(
                    "https://example.com/data.txt", str(output_file)
                )

            self.assertTrue(result.success)
            self.assertEqual(result.size_bytes, 11)
            self.assertEqual(output_file.read_bytes(), b"sample-data")

    def test_download_data_rejects_blank_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "data.txt"
            result = ComfortDownloadTeam().download_data("   ", str(output_file))

        self.assertFalse(result.success)
        self.assertIn("URL is required", result.message)

    def test_download_data_fails_for_empty_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "data.txt"
            with patch(
                "personal_dataops_agent.team.urlopen",
                return_value=_FakeResponse(b""),
            ):
                result = ComfortDownloadTeam().download_data(
                    "https://example.com/data.txt", str(output_file)
                )

        self.assertFalse(result.success)
        self.assertIn("empty or missing", result.message)


if __name__ == "__main__":
    unittest.main()
