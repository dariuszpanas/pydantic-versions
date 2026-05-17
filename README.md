# pydantic-versions

Bring version control and history to your Pydantic schemas.

This project is in the research and design phase. The initial repository structure is ready for package development, documentation, tests, and CI, but the public API is intentionally not defined yet.

## Development

Install dependencies:

```bash
uv sync
```

Run the main checks:

```bash
make ci
```

Useful commands:

- `make format`: format with Ruff.
- `make lint`: lint and auto-fix with Ruff.
- `make typecheck`: run `ty`.
- `make test`: run pytest.
- `make docs-build`: build the docs site.
