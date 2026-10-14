# Contributing to siyi-sdk

Thank you for your interest in contributing to the SIYI SDK project!

## Development Setup

### Prerequisites

- Python 3.10 or higher
- git

### Initial Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/ncsuarc/siyi_sdk.git
   cd siyi_sdk
   ```

2. Create the development environment, with hatch (as CI does) or uv:
   ```bash
   hatch env create
   # or
   uv sync --extra remote-server --extra web --extra tracking
   ```

3. Install pre-commit hooks:
   ```bash
   pre-commit install
   ```

## Running Tests

To run the full test suite with coverage:

```bash
hatch run test:cov
```

To run tests without coverage:

```bash
hatch run test:test
```

To run a specific test file or marker:

```bash
hatch run test:test tests/path/to/test_file.py -k "test_function"
```

## Code Quality

### Linting and Formatting

Run all linting checks:

```bash
hatch run lint:lint
```

Auto-fix linting and formatting issues:

```bash
hatch run lint:fmt
```

### Type Checking

Run strict type checking:

```bash
hatch run lint:typecheck
```

## Submitting Changes

1. Create a feature branch from `siyi-sdk-v2` (the main branch):
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes and commit with clear, descriptive messages:
   ```bash
   git commit -m "Description of changes"
   ```

3. Ensure all tests pass and code quality checks are clean:
   ```bash
   hatch run lint:lint
   hatch run lint:typecheck
   hatch run test:cov
   ```

4. Update `CHANGELOG.md` by adding your changes to the `[Unreleased]` section
   following the [Keep a Changelog](https://keepachangelog.com/) format.

5. Push your branch and create a pull request:
   ```bash
   git push origin feature/your-feature-name
   ```

6. Ensure CI/CD pipeline passes all checks before requesting review.

## Project Layout

| Path | Contents |
| --- | --- |
| `siyi_sdk/client.py`, `siyi_sdk/commands/`, `siyi_sdk/protocol/`, `siyi_sdk/transport/` | `SIYIClient` and the SIYI binary protocol |
| `siyi_sdk/stream/` | RTSP video backends |
| `siyi_sdk/tracking/` | Point lock, calibration, firmware tracking |
| `siyi_sdk/remote/` | Camera service and `RemoteCamera`: `session.py` (camera, video, lock, deadman), `server.py` (websocket, JSON-RPC dispatch, control), `client.py` (ground side), `protocol.py` (wire format, errors), `registry.py` (commands allowed by name) |
| `web_ui/` | Debugging dashboard (not part of the package) |
| `docs/` | User guides; `docs/remote.md` documents the RPC interface, `docs/deployment.md` the Iris/Shimmer setup |

### Exposing a camera command over RPC

1. Add the `SIYIClient` method to `_GROUPS` in `siyi_sdk/remote/registry.py`. Its signature becomes the RPC parameter schema, and `get_*` methods are open to observers. Leave it out of `REMOTE_EXCLUDED` unless it keeps the camera moving until another command (those need a setpoint with a deadman instead).
2. Add a typed wrapper with the same signature to `RemoteCamera` in `siyi_sdk/remote/client.py`.
3. List it in the RPC reference in `docs/remote.md`.

Service-level methods (not camera commands) go in `CameraServer._build_methods` in `siyi_sdk/remote/server.py`, with the work done in `CameraSession`.

## Code Style Guidelines

- **Line length**: 100 characters
- **Docstrings**: Google style for all public classes and functions
- **Type annotations**: Required on all functions and methods (mypy strict mode)
- **Imports**: Sorted and organized per ruff isort rules
- **Formatting**: Black formatter with 100-char line length

## Commit Message Guidelines

Write clear, concise commit messages in the format:

```
<type>: <subject>

<body>
```

Where `<type>` can be:
- `feat`: A new feature
- `fix`: A bug fix
- `docs`: Documentation changes
- `refactor`: Code refactoring without feature changes
- `test`: Adding or updating tests
- `chore`: Build, CI, or dependency updates

## Questions?

If you have questions or need clarification, please open an issue or reach out to
the maintainers.

Thank you for contributing!
