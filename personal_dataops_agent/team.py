"""Simple agent team that helps users download data comfortably."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


@dataclass(frozen=True)
class DownloadResult:
    success: bool
    message: str
    output_path: str
    size_bytes: int = 0


class PlannerAgent:
    """Builds a clear execution plan for downloading a file."""

    @staticmethod
    def plan(url: str, output_path: str) -> tuple[str, Path]:
        if not url.strip():
            raise ValueError("URL is required.")
        destination = Path(output_path)
        if destination.is_dir():
            raise ValueError("Output path must be a file path, not a directory.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        return url.strip(), destination


class DownloadAgent:
    """Handles fetching bytes from a URL and saving them to disk."""

    @staticmethod
    def download(url: str, destination: Path) -> int:
        with urlopen(url) as response:  # nosec B310 - user controlled URL by design
            content = response.read()
        destination.write_bytes(content)
        return len(content)


class ValidationAgent:
    """Validates that downloaded data exists and is not empty."""

    @staticmethod
    def validate(destination: Path) -> bool:
        return destination.exists() and destination.stat().st_size > 0


class ComfortDownloadTeam:
    """Coordinates planner, downloader, and validator agents."""

    def __init__(self) -> None:
        self.planner = PlannerAgent()
        self.downloader = DownloadAgent()
        self.validator = ValidationAgent()

    def download_data(self, url: str, output_path: str) -> DownloadResult:
        try:
            planned_url, destination = self.planner.plan(url, output_path)
            size = self.downloader.download(planned_url, destination)
        except (OSError, URLError, ValueError) as exc:
            return DownloadResult(
                success=False,
                message=f"Download failed: {exc}",
                output_path=output_path,
            )

        if not self.validator.validate(destination):
            return DownloadResult(
                success=False,
                message="Download failed: downloaded file is empty or missing.",
                output_path=str(destination),
            )

        return DownloadResult(
            success=True,
            message="Download completed successfully.",
            output_path=str(destination),
            size_bytes=size,
        )
