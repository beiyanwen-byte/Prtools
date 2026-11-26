# GitHub PR Desk

A Python/Tk desktop tool for reviewing GitHub repository branches, pull requests,
tags, and read-only workflow deployment results. It supports Windows repositories
and WSL repositories when the required Git and Python runtimes are available.

## Start

On Windows, double-click `打开PR工具.cmd`. Python 3 with Tk support and Git must be
installed. Select a workspace, then select a Git repository in the left pane.
The application uses Git's configured credential helper; an alternative token can
be supplied through the desktop dialog when needed.

For setup, token permissions, operations, local privacy and troubleshooting,
see [操作说明](docs/操作说明.md).

## Project layout

- `app/`: desktop application, GitHub client and supporting modules.
- `tests/`: offline tests and synthetic fixtures.
- `docs/操作说明.md`: desktop setup and usage.

To run the offline suite from the project directory:

```text
python -B -m unittest discover -s tests -t .
```

Tests use synthetic data and mocked network operations unless a test explicitly
states that it is opt-in. Never store tokens, local preferences, machine-specific
paths or private repository data in tracked files.
