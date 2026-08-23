from __future__ import annotations

import ast
import inspect
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

import pydantic_versions as public_api

REPOSITORY = Path(__file__).resolve().parents[2]
API_REFERENCE = REPOSITORY / "docs" / "reference" / "api.md"

_EXECUTABLE_BLOCK = re.compile(
    r"<!-- pv-doc-test: (?P<session>[a-z0-9-]+) -->\n"
    r"```python\n(?P<source>.*?)\n```",
    re.DOTALL,
)
_API_SIGNATURE_BLOCK = re.compile(
    r"<!-- pv-api-signature: (?P<name>[a-z_]+) -->\n"
    r"```python\n(?P<source>.*?)\n```",
    re.DOTALL,
)
_FENCE_OPEN = re.compile(r"^ {0,3}(?P<delimiter>`{3,}|~{3,})(?P<info>.*)$")
_EXCEPTION_ITEM = re.compile(
    r"^(?P<indent> *)- `(?P<name>[A-Za-z][A-Za-z0-9]*)`$",
    re.MULTILINE,
)

_CANONICAL_DOCUMENTS = (
    (Path("README.md"), {"readme-example": 1}),
    (Path("docs/guide/getting-started.md"), {"getting-started": 6}),
    (Path("docs/guide/external-families.md"), {"external-families": 6}),
    (Path("docs/guide/rendering.md"), {"rendering": 4}),
    (
        Path("docs/guide/adoption-guidance.md"),
        {"adoption-version-fields": 2},
    ),
    (
        Path("docs/guide/complex-config-example.md"),
        {"complex-plain": 1, "complex-versioned": 6},
    ),
)


def _executable_sessions(path: Path) -> dict[str, list[str]]:
    sessions: defaultdict[str, list[str]] = defaultdict(list)
    for match in _EXECUTABLE_BLOCK.finditer(path.read_text(encoding="utf-8")):
        sessions[match.group("session")].append(match.group("source"))
    return dict(sessions)


def _python_fences(text: str) -> tuple[tuple[int, str], ...]:
    blocks: list[tuple[int, str]] = []
    active_delimiter: str | None = None
    active_language = ""
    source_line = 0
    source_lines: list[str] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        if active_delimiter is None:
            match = _FENCE_OPEN.fullmatch(line)
            if match is None:
                continue
            active_delimiter = match.group("delimiter")
            info = match.group("info").strip()
            active_language = info.split(maxsplit=1)[0] if info else ""
            source_line = line_number + 1
            source_lines = []
            continue

        stripped = line.lstrip(" ")
        indentation = len(line) - len(stripped)
        candidate = stripped.rstrip(" \t")
        is_closing = (
            indentation <= 3
            and len(candidate) >= len(active_delimiter)
            and set(candidate) == {active_delimiter[0]}
        )
        if not is_closing:
            if active_language == "python":
                source_lines.append(line)
            continue

        if active_language == "python":
            blocks.append((source_line, "\n".join(source_lines)))
        active_delimiter = None
        active_language = ""
        source_lines = []

    if active_delimiter is not None and active_language == "python":
        raise ValueError(f"unclosed Python fence at line {source_line - 1}")
    return tuple(blocks)


@pytest.mark.parametrize(
    ("relative_path", "expected_counts"),
    _CANONICAL_DOCUMENTS,
    ids=(
        "readme",
        "getting-started",
        "external-families",
        "rendering",
        "adoption-guidance",
        "complex-config",
    ),
)
def test_canonical_documentation_examples_execute_from_the_installed_package(
    relative_path: Path,
    expected_counts: dict[str, int],
    tmp_path: Path,
) -> None:
    document = REPOSITORY / relative_path
    sessions = _executable_sessions(document)

    assert {name: len(blocks) for name, blocks in sessions.items()} == expected_counts
    for session, blocks in sessions.items():
        source = "\n\n".join(blocks)
        working_directory = tmp_path / session
        working_directory.mkdir()
        completed = subprocess.run(
            [sys.executable, "-I", "-c", source],
            cwd=working_directory,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert completed.returncode == 0, (
            f"{relative_path.as_posix()} session {session!r} failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


def _annotation_source(annotation: Any) -> str | None:
    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, str):
        return annotation
    return inspect.formatannotation(annotation)


def _runtime_signature_shape(
    callable_: Any,
) -> tuple[tuple[str, str, str | None, str | None], ...]:
    return tuple(
        (
            name,
            parameter.kind.name,
            _annotation_source(parameter.annotation),
            None if parameter.default is inspect.Parameter.empty else repr(parameter.default),
        )
        for name, parameter in inspect.signature(callable_).parameters.items()
    )


def _documented_signature_shape(
    source: str,
) -> tuple[str, tuple[tuple[str, str, str | None, str | None], ...], str]:
    parsed = ast.parse(source)
    assert len(parsed.body) == 1
    function = parsed.body[0]
    assert isinstance(function, ast.FunctionDef)

    arguments = function.args
    positional = (*arguments.posonlyargs, *arguments.args)
    first_default = len(positional) - len(arguments.defaults)
    shape: list[tuple[str, str, str | None, str | None]] = []
    for index, argument in enumerate(positional):
        kind = "POSITIONAL_ONLY" if index < len(arguments.posonlyargs) else "POSITIONAL_OR_KEYWORD"
        default = arguments.defaults[index - first_default] if index >= first_default else None
        shape.append(
            (
                argument.arg,
                kind,
                None if argument.annotation is None else ast.unparse(argument.annotation),
                None if default is None else ast.unparse(default),
            )
        )
    if arguments.vararg is not None:
        shape.append(
            (
                arguments.vararg.arg,
                "VAR_POSITIONAL",
                None
                if arguments.vararg.annotation is None
                else ast.unparse(arguments.vararg.annotation),
                None,
            )
        )
    shape.extend(
        (
            argument.arg,
            "KEYWORD_ONLY",
            None if argument.annotation is None else ast.unparse(argument.annotation),
            None if default is None else ast.unparse(default),
        )
        for argument, default in zip(
            arguments.kwonlyargs,
            arguments.kw_defaults,
            strict=True,
        )
    )
    if arguments.kwarg is not None:
        shape.append(
            (
                arguments.kwarg.arg,
                "VAR_KEYWORD",
                None
                if arguments.kwarg.annotation is None
                else ast.unparse(arguments.kwarg.annotation),
                None,
            )
        )
    assert function.returns is not None
    return function.name, tuple(shape), ast.unparse(function.returns)


def test_documented_decorator_signatures_match_the_public_api() -> None:
    reference = API_REFERENCE.read_text(encoding="utf-8")
    expected_names = {
        "migration",
        "schema_version",
        "schema_versions",
        "versioned_schema",
    }
    blocks: defaultdict[str, list[str]] = defaultdict(list)
    for match in _API_SIGNATURE_BLOCK.finditer(reference):
        blocks[match.group("name")].append(match.group("source"))

    assert {name: len(sources) for name, sources in blocks.items()} == dict.fromkeys(
        expected_names,
        1,
    )
    documented = {name: _documented_signature_shape(sources[0]) for name, sources in blocks.items()}

    for name, (declared_name, signature, return_annotation) in documented.items():
        assert declared_name == name
        runtime = getattr(public_api, name)
        assert signature == _runtime_signature_shape(runtime)
        assert return_annotation == _annotation_source(inspect.signature(runtime).return_annotation)


def test_documented_package_version_is_a_single_typed_api_entry() -> None:
    reference = API_REFERENCE.read_text(encoding="utf-8")
    section = reference.split("## Package metadata", maxsplit=1)[1].split(
        "\n## ",
        maxsplit=1,
    )[0]

    assert section.count("`__version__: str`") == 1


def test_python_fence_scanner_handles_rendered_variants() -> None:
    document = """```text
```python
ignored =
```
```python title="Example"
first = 1
````
~~~python linenums="1"
second = 2
~~~
"""

    assert _python_fences(document) == (
        (6, "first = 1"),
        (9, "second = 2"),
    )
    with pytest.raises(ValueError, match="unclosed Python fence at line 1"):
        _python_fences("~~~python\nvalue = 1")


def test_all_rendered_python_fences_are_syntactically_valid() -> None:
    documents = (
        REPOSITORY / "README.md",
        *sorted((REPOSITORY / "docs").rglob("*.md")),
    )
    failures: list[str] = []
    for document in documents:
        text = document.read_text(encoding="utf-8")
        relative_path = document.relative_to(REPOSITORY).as_posix()
        try:
            blocks = _python_fences(text)
        except ValueError as exc:
            failures.append(f"{relative_path}: {exc}")
            continue
        for line_number, source in blocks:
            try:
                ast.parse(
                    source,
                    filename=relative_path,
                )
            except SyntaxError as exc:
                failure_line = line_number + (exc.lineno or 1) - 1
                failures.append(f"{relative_path}:{failure_line}: {exc.msg}")

    assert not failures, "Invalid Python documentation fences:\n" + "\n".join(failures)


def test_documented_exception_hierarchy_matches_the_public_api() -> None:
    reference = API_REFERENCE.read_text(encoding="utf-8")
    hierarchy = reference.split("## Exceptions", maxsplit=1)[1].split(
        "See the [stability",
        maxsplit=1,
    )[0]
    expected_names = (
        "SchemaVersionError",
        "SchemaCompilationError",
        "UnsupportedWireModelError",
        "SchemaFamilySelectionError",
        "IrreversibleTransitionError",
        "MissingSchemaVersionError",
        "UnknownSchemaVersionError",
        "DuplicateSchemaVersionError",
        "InvalidMigrationError",
    )
    documented_parents: dict[str, str] = {}
    ancestors: list[str] = []
    for match in _EXCEPTION_ITEM.finditer(hierarchy):
        indentation = len(match.group("indent"))
        assert indentation % 2 == 0
        depth = indentation // 2
        assert depth <= len(ancestors)

        name = match.group("name")
        assert name not in documented_parents
        documented_parents[name] = "Exception" if depth == 0 else ancestors[depth - 1]
        ancestors = [*ancestors[:depth], name]

    runtime_parents = {
        name: getattr(public_api, name).__bases__[0].__name__ for name in expected_names
    }
    assert documented_parents == runtime_parents
