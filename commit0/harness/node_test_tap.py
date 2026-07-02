from __future__ import annotations

import re
from typing import Optional


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_JSBT_TREE_CHARS = "├│└─ \t"

_JSBT_RESULT_LINE = re.compile(
    r"^#\s+(?P<tree>[" + re.escape(_JSBT_TREE_CHARS) + r"]*)"
    r"(?P<name>[^:\r\n]+?):\s*(?P<symbol>[✓☓✗])\s*$"
)

_JSBT_PENDING_LINE = re.compile(
    r"^#\s+(?P<tree>[" + re.escape(_JSBT_TREE_CHARS) + r"]*)"
    r"(?P<name>[^:\r\n]+?):\s*☆\s*$"
)


_OK_LINE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<status>ok|not ok)\s+(?P<num>\d+)\s*(?:-\s*)?(?P<name>[^\r\n#]+?)"
    r"(?:\s*#\s*(?P<directive>SKIP|TODO)(?:\s+(?P<reason>.*))?)?"
    r"\s*$",
    re.IGNORECASE,
)

_SUBTEST_LINE = re.compile(
    r"^\s*#\s*Subtest:\s*(?P<name>.+?)\s*$",
    re.IGNORECASE,
)

_DURATION_LINE = re.compile(r"^\s*duration_ms:\s*([\d.]+)\s*$")

_BAIL_OUT = re.compile(r"^\s*Bail\s+out!", re.IGNORECASE)

_YAML_START = re.compile(r"^\s*---\s*$")
_YAML_END = re.compile(r"^\s*\.\.\.\s*$")


def _clean_name(raw: str) -> str:
    name = raw.strip()
    if name.startswith("- "):
        name = name[2:].strip()
    return name


def parse_tap_output(text: str) -> list[dict]:
    if not text:
        return []

    subtest_stack: list[str] = []
    prev_indent = -1
    in_yaml = False
    pending_duration: Optional[float] = None

    results: list[dict] = []
    last_index = -1

    for raw_line in text.splitlines():
        if in_yaml:
            if _DURATION_LINE.match(raw_line):
                m = _DURATION_LINE.match(raw_line)
                if m and last_index >= 0:
                    try:
                        results[last_index]["duration_ms"] = float(m.group(1))
                    except (ValueError, IndexError):
                        pass
            if _YAML_END.match(raw_line):
                in_yaml = False
            continue

        if _YAML_START.match(raw_line):
            in_yaml = True
            continue

        sub = _SUBTEST_LINE.match(raw_line)
        if sub:
            leading = len(raw_line) - len(raw_line.lstrip())
            while subtest_stack and prev_indent >= 0 and leading <= prev_indent:
                subtest_stack.pop()
                prev_indent -= 4
            subtest_stack.append(sub.group("name").strip())
            prev_indent = leading
            continue

        ok = _OK_LINE.match(raw_line)
        if ok:
            leading = len(ok.group("indent") or "")
            while subtest_stack and prev_indent > leading:
                subtest_stack.pop()
                prev_indent -= 4

            name = _clean_name(ok.group("name"))
            status = "passed" if ok.group("status").lower() == "ok" else "failed"
            directive = ok.group("directive")
            if directive and directive.upper() in ("SKIP", "TODO"):
                status = "skipped"

            full_name_parts = subtest_stack.copy()
            if full_name_parts and full_name_parts[-1] == name:
                full_name = " > ".join(full_name_parts)
            elif full_name_parts:
                full_name = " > ".join(full_name_parts + [name])
            else:
                full_name = name

            results.append({
                "name": name,
                "full_name": full_name,
                "status": status,
                "duration_ms": pending_duration or 0.0,
            })
            last_index = len(results) - 1
            pending_duration = None
            continue

    return results


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _tree_depth(tree: str) -> int:
    return sum(1 for c in tree if c in "│├└─") // 2


def parse_jsbt_comments(text: str) -> list[dict]:
    if not text:
        return []

    scope_stack: list[tuple[int, str]] = []
    results: list[dict] = []
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = _strip_ansi(raw_line)

        m = _JSBT_RESULT_LINE.match(line)
        if m:
            depth = _tree_depth(m.group("tree"))
            name = m.group("name").strip()
            symbol = m.group("symbol")
            status = "passed" if symbol == "✓" else "failed"
            while scope_stack and scope_stack[-1][0] >= depth:
                scope_stack.pop()
            full_name_parts = [n for _, n in scope_stack] + [name]
            full_name = " > ".join(full_name_parts)
            if full_name in seen:
                continue
            seen.add(full_name)
            results.append({
                "name": name,
                "full_name": full_name,
                "status": status,
                "duration_ms": 0.0,
                "source": "jsbt",
            })
            continue

        m = _JSBT_PENDING_LINE.match(line)
        if m:
            depth = _tree_depth(m.group("tree"))
            name = m.group("name").strip()
            while scope_stack and scope_stack[-1][0] >= depth:
                scope_stack.pop()
            scope_stack.append((depth, name))
            continue

    return results


def tap_to_jest_report_shape(text: str, file_path: str = "unknown") -> dict:
    parsed = parse_tap_output(text)
    jsbt_parsed = parse_jsbt_comments(text)

    assertion_results = []
    seen: set[str] = set()

    for entry in jsbt_parsed:
        if entry["full_name"] in seen:
            continue
        seen.add(entry["full_name"])
        assertion_results.append({
            "fullName": entry["full_name"],
            "title": entry["name"],
            "status": entry["status"],
            "duration": entry.get("duration_ms", 0.0),
        })

    for entry in parsed:
        if entry["full_name"] in seen:
            continue
        if jsbt_parsed and entry["full_name"].startswith(("/", "src/")):
            continue
        seen.add(entry["full_name"])
        assertion_results.append({
            "fullName": entry["full_name"],
            "title": entry["name"],
            "status": entry["status"],
            "duration": entry.get("duration_ms", 0.0),
        })

    return {
        "testResults": [{
            "testFilePath": file_path,
            "assertionResults": assertion_results,
            "failureMessage": None,
        }],
    }


def list_tap_test_names(text: str) -> list[str]:
    jsbt = parse_jsbt_comments(text)
    if jsbt:
        return [e["full_name"] for e in jsbt]
    return [e["full_name"] for e in parse_tap_output(text)]


def detect_tap_suite_crash(text: str) -> bool:
    if not text:
        return True
    if _BAIL_OUT.search(text):
        return True
    parsed = parse_tap_output(text)
    return len(parsed) == 0


_STATIC_TEST_PATTERN = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<kind>describe|it|test)"
    r"\s*\(\s*"
    r"(?P<quote>['\"`])"
    r"(?P<name>(?:\\.|(?!(?P=quote)).)*?)"
    r"(?P=quote)"
)


def extract_test_names_static(content: str) -> list[str]:
    if not content:
        return []

    describe_stack: list[tuple[int, str]] = []
    tests: list[str] = []
    seen: set[str] = set()

    for line in content.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("//") or stripped.startswith("*"):
            continue
        m = _STATIC_TEST_PATTERN.match(line)
        if not m:
            continue
        raw_indent = m.group("indent")
        indent = len(raw_indent.expandtabs(4))
        kind = m.group("kind")
        name = m.group("name").replace("\\'", "'").replace('\\"', '"')

        while describe_stack and describe_stack[-1][0] >= indent:
            describe_stack.pop()

        if kind == "describe":
            describe_stack.append((indent, name))
        else:
            parts = [n for _, n in describe_stack] + [name]
            full = " > ".join(parts)
            if full not in seen:
                seen.add(full)
                tests.append(full)

    return tests
