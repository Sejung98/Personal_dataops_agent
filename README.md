# Personal_dataops_agent

A lightweight **agent team** that helps you comfortably download data files.

## Agent team

- **PlannerAgent**: checks URL/output path and prepares a safe destination.
- **DownloadAgent**: downloads bytes from the URL to the output file.
- **ValidationAgent**: verifies the downloaded file exists and is not empty.

## Usage

```bash
python -m personal_dataops_agent.cli "https://example.com/data.csv" "./downloads/data.csv"
```

The command prints JSON output with status, message, path, and size.
