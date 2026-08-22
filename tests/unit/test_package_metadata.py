from pathlib import Path

ROOT = Path(__file__).parents[2]
README = ROOT / "README.md"
DOCS_URL = "https://pydantic-versions.readthedocs.io/en/latest/"
DOCS_BADGE_URL = "https://img.shields.io/readthedocs/pydantic-versions/latest.svg"
REDIRECTING_BADGE_URL = "https://readthedocs.org/projects/pydantic-versions/badge/?version=latest"


def test_readme_uses_a_non_redirecting_documentation_badge_for_pypi() -> None:
    readme = README.read_text(encoding="utf-8")

    assert f'<a href="{DOCS_URL}"><img src="{DOCS_BADGE_URL}"' in readme
    assert REDIRECTING_BADGE_URL not in readme
