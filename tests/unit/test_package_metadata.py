import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
README = ROOT / "README.md"
LLMS_TXT = ROOT / "docs" / "llms.txt"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
RENDERED_CONTRIBUTING = ROOT / "docs" / "contributing.md"
DOCS_URL = "https://pydantic-versions.readthedocs.io/en/latest/"
DOCS_ORIGIN = "https://pydantic-versions.readthedocs.io/"
DOCS_BADGE_URL = "https://img.shields.io/readthedocs/pydantic-versions/latest.svg"
REDIRECTING_BADGE_URL = "https://readthedocs.org/projects/pydantic-versions/badge/?version=latest"
MARKDOWN_LINK = re.compile(r"\[[^]]+\]\((?P<url>[^)\s]+)\)")


def test_runtime_dependencies_match_direct_production_requirements() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["dependencies"] == [
        "annotated-types>=0.6.0",
        "pydantic>=2.12.3,<3.0",
        "typing-extensions>=4.14.1",
    ]


def test_readme_uses_a_non_redirecting_documentation_badge_for_pypi() -> None:
    readme = README.read_text(encoding="utf-8")

    assert f'<a href="{DOCS_URL}"><img src="{DOCS_BADGE_URL}"' in readme
    assert REDIRECTING_BADGE_URL not in readme


def test_llms_index_links_to_existing_documentation_sources() -> None:
    llms_index = LLMS_TXT.read_text(encoding="utf-8")

    assert llms_index.startswith("# pydantic-versions\n\n>")
    linked_pages = [
        match.group("url")
        for match in MARKDOWN_LINK.finditer(llms_index)
        if match.group("url").startswith(DOCS_ORIGIN)
    ]
    assert len(linked_pages) >= 8

    for linked_page_url in linked_pages:
        assert linked_page_url.startswith(DOCS_URL), linked_page_url
        linked_page = linked_page_url.removeprefix(DOCS_URL)
        assert linked_page.endswith("/") and "?" not in linked_page and "#" not in linked_page
        source = ROOT / "docs" / Path(*linked_page.removesuffix("/").split("/"))
        assert source.with_suffix(".md").is_file(), linked_page_url


def test_rendered_contributor_guide_matches_the_repository_guide() -> None:
    rendered = RENDERED_CONTRIBUTING.read_text(encoding="utf-8")
    repository = CONTRIBUTING.read_text(encoding="utf-8")

    assert rendered == repository
