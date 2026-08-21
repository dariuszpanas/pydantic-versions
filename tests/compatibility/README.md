# Cross-release compatibility fixtures

The `v0.3.0.json` golden records payloads and inspection artifacts produced by
the immutable `pydantic-versions` 0.3.0 release. It is a reviewed compatibility
contract, not a disposable snapshot.

Regenerate it only from the exact released package in an isolated environment:

```powershell
uv run --isolated --no-project --with "pydantic-versions==0.3.0" python tests/compatibility/v0_3_0_contract.py --write
```

Check the committed artifact with the current checkout:

```powershell
uv run python tests/compatibility/v0_3_0_contract.py --check
```

The write command refuses a different package version. Review the resulting
human-readable diff before committing it. Never regenerate solely to make a
compatibility failure disappear: either preserve the stable contract or
document and version the intentional change according to the stability policy.
