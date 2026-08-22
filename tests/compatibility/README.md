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

Current code intentionally adds two steps to the frozen artifact: a conditional
`$.credentials` nested step in `validate_v1` and another in `render_v1_lossy`.
The checker applies that reviewed additive overlay without modifying the 0.3.0
fixture, then compares the exact rendered artifact, including dictionary key
order and step positions. It returns success only for those two additions; any
other output remains an unexplained compatibility delta.

The write command refuses a different package version. Review the resulting
human-readable diff before committing it. Never regenerate solely to make a
compatibility failure disappear: either preserve the stable contract or
document and version the intentional change according to the stability policy.

The immutable artifact also preserves 0.3.0's redundant family-owned version
metadata inside nested child payloads. Before 1.0, rendering was corrected to
let the parent mapping own that selection consistently for direct and
collection children. The checker derives the current expected contract by
removing only those three reviewed nested discriminator paths; every other
payload and inspection value must still match the released golden exactly.
