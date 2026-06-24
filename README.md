# M-Claw

M-Claw is a cross-platform desktop CLI AI agent. It provides a terminal chat
interface, provider-based model access, tool calling, runtime-aware file and
process operations, memory support, Skills integration, and optional channel
integrations.

## Requirements

- Python 3.11 or newer

## Install

Install from this local source tree in editable mode:

```bash
pip install -e .
```

This installs M-Claw's default desktop dependency set from `pyproject.toml`.
The project is not currently published to PyPI, so do not use
`pip install m-claw` unless a PyPI release has been created.

After installation, start the CLI with:

```bash
mclaw
```

## Open-Source Review Notes

This source package includes an open-source cleanup audit in
`OPEN_SOURCE_CODE_AUDIT.md`. Review that file before publishing a release,
especially the remaining packaging and test-readiness items.

## License

Apache-2.0. See `LICENSE` for the full license text.
