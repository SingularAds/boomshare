"""Keep the documentation honest.

`docs/code-blueprint.md` cites exact file paths and line numbers. Those rot the
moment anyone inserts a line, and a blueprint that points at the wrong function
is worse than none at all - so the build checks them.

If one of these fails, the fix is to correct the document, not to delete the
test. The whole value of the blueprint is that its references can be trusted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# `docs/` is gitignored, so a clean checkout (CI, a fresh clone, the build
# image) will not have it. These checks keep the documentation honest for
# whoever is editing it locally; where the docs are absent there is nothing to
# verify, so skip the whole module rather than error at import time.
if not DOCS.is_dir():
    pytest.skip("docs/ not present in this checkout", allow_module_level=True)

_DEF = re.compile(r"\s*(?:async )?(?:def|class) (\w+)")
#: `name :123`, `name()` :123, `` `name` `` :123
_SYMBOL_REF = re.compile(r"`?(\w+)`?(?:\(\))? :(\d+)")
#: `app/services/foo.py:123`
_PATH_REF = re.compile(r"((?:app|tests|scripts)/[A-Za-z0-9_./-]+\.py):(\d+)")
#: any repo path in backticks
_PATH = re.compile(r"`((?:app|tests|scripts|docs|migrations)/[A-Za-z0-9_./-]+\.(?:py|md))`")
#: relative markdown links between docs
_LINK = re.compile(r"\[[^\]]+\]\((?!https?:)([^)#]+)(?:#[^)]*)?\)")


def _definitions() -> dict[str, set[tuple[str, int]]]:
    """symbol -> {(file, line)} for every def/class under app/."""
    found: dict[str, set[tuple[str, int]]] = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _DEF.match(line)
            if match:
                found.setdefault(match.group(1), set()).add((rel, number))
    return found


DEFINITIONS = _definitions()
BLUEPRINT = (DOCS / "code-blueprint.md").read_text(encoding="utf-8")


def _files_named_on(line: str) -> list[str]:
    """Which source files a documentation line is talking about."""
    names = re.findall(r"([A-Za-z0-9_./]+\.py)", line)
    matches: list[str] = []
    for name in names:
        clean = name.strip("`")
        for file, _ in {k for v in DEFINITIONS.values() for k in v}:
            if file.endswith(clean) and file not in matches:
                matches.append(file)
    return matches


class TestBlueprintReferences:
    def test_every_symbol_line_reference_is_correct(self):
        """`set_stage :225` must actually be `set_stage` on line 225."""
        wrong: list[str] = []

        for line in BLUEPRINT.splitlines():
            if " :" not in line:
                continue
            scoped = _files_named_on(line)

            for symbol, number in _SYMBOL_REF.findall(line):
                places = DEFINITIONS.get(symbol)
                if not places:
                    continue  # not an app symbol - e.g. a bare word before a number

                if scoped:
                    expected = {ln for file, ln in places if file in scoped}
                    if not expected:
                        continue
                else:
                    expected = {ln for _, ln in places}

                if int(number) not in expected:
                    wrong.append(f"{symbol} :{number} -> actually {sorted(expected)}")

        assert not wrong, "stale line references in code-blueprint.md:\n  " + "\n  ".join(wrong)

    def test_every_path_line_reference_is_a_definition(self):
        """`app/worker/jobs.py:37` must land on a def, class or route."""
        wrong: list[str] = []
        allowed = re.compile(r"(async )?def |class |router = |@router|@app|UPDATE|SELECT")

        for path_str, number in _PATH_REF.findall(BLUEPRINT):
            path = ROOT / path_str
            if not path.is_file():
                wrong.append(f"{path_str}:{number} (no such file)")
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            if int(number) > len(lines):
                wrong.append(f"{path_str}:{number} (past end of file)")
                continue
            if not allowed.match(lines[int(number) - 1].strip()):
                wrong.append(f"{path_str}:{number} -> {lines[int(number) - 1].strip()[:60]!r}")

        assert not wrong, "bad path references in code-blueprint.md:\n  " + "\n  ".join(wrong)


class TestDocsReferToRealFiles:
    @pytest.mark.parametrize(
        "doc",
        sorted(p.name for p in DOCS.glob("*.md")) + ["../README.md"],
    )
    def test_referenced_source_files_exist(self, doc):
        text = (DOCS / doc).read_text(encoding="utf-8")
        missing = [p for p in set(_PATH.findall(text)) if not (ROOT / p).is_file()]
        assert not missing, f"{doc} references files that do not exist: {missing}"

    @pytest.mark.parametrize("doc", sorted(p.name for p in DOCS.glob("*.md")))
    def test_relative_links_resolve(self, doc):
        text = (DOCS / doc).read_text(encoding="utf-8")
        broken = []
        for target in set(_LINK.findall(text)):
            if not (DOCS / target).resolve().exists():
                broken.append(target)
        assert not broken, f"{doc} has broken links: {broken}"


class TestReadmeStaysTrue:
    def test_the_quoted_test_count_matches_reality(self):
        """The README advertises a test count; it should not drift."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r"pytest\s+#\s*(\d+) tests", readme.replace("  ", " "))
        if match is None:
            pytest.skip("README does not advertise a test count")

        claimed = int(match.group(1))
        actual = sum(
            len(re.findall(r"^\s*(?:async )?def test_", p.read_text(encoding="utf-8"), re.M))
            for p in (ROOT / "tests").glob("test_*.py")
        )
        # Parametrised tests expand, so the real number is >= the definitions.
        assert actual <= claimed, (
            f"README claims {claimed} tests but there are already {actual} test "
            "functions before parametrisation - update the README"
        )
