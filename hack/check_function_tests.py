#!/usr/bin/env python3
# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Check the function unit tests against the rules in CONTRIBUTING.md's Tests section.

This checks every functions/*/tests/test_*.py, or the files it's given, against
the rules an AST can decide without judgement: how a table and its test are
laid out, a case's fields, name and reason, the assertion message, the calls
that derive or mutate a value, the parameters a helper takes, and the names of
tests. Whether a reason is true is for a reviewer.

Where a test has a real need to depart from a rule, an escape on the line the
checker reports, with a stated reason, accepts the departure:

    got = case.template.model_copy(deep=True)  # noqa: MPT401  # fn edits it in place.
"""

import ast
import collections.abc
import io
import pathlib
import re
import sys
import tokenize
import typing

REPO = pathlib.Path(__file__).resolve().parents[1]

# A table's name. COMPOSE_CASES is run by test_compose.
TABLE = re.compile(r"[A-Z][A-Z0-9_]*_CASES")

CAMEL_CASE = re.compile(r"[A-Z][A-Za-z0-9]*")
MAX_NAME_WORDS = 4
MAX_TEST_WORDS = 3

# The capital letters that start a word: one that doesn't follow another
# capital, or one that starts a lowercase run. An acronym such as the GKE in
# GKEFirstPass counts as one word, as it does in Go's names.
WORD = re.compile(r"(?<![A-Z])[A-Z]|[A-Z](?=[a-z])")

# Sentence-ending punctuation with more text after it.
SENTENCE_BREAK = re.compile(r"[.!?]\s+\S")

# An escape: noqa, its codes, then the reason, as in the docstring above. The
# codes are those ruff's noqa takes, so one comment can carry ruff's and ours.
NOQA = re.compile(r"#\s*noqa:\s*(?P<codes>[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*)(?P<reason>.*)")

COPIES = {"copy.copy", "copy.deepcopy"}
DERIVING_METHODS = {"CopyFrom", "MergeFrom", "SetInParent", "model_copy"}


class Violation(typing.NamedTuple):
    """A rule a test file breaks, at a line."""

    line: int
    code: str
    message: str


def is_call(node: ast.AST, func: str) -> bool:
    """Whether node calls func, written as its dotted name."""
    return isinstance(node, ast.Call) and ast.unparse(node.func) == func


def literal(node: ast.expr | None) -> str | None:
    """node's value, if it's a string literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def table_name(stmt: ast.stmt) -> str | None:
    """The name of the table stmt assigns, if it assigns one."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
    elif isinstance(stmt, ast.AnnAssign):
        target = stmt.target
    else:
        return None
    if isinstance(target, ast.Name) and TABLE.fullmatch(target.id):
        return target.id
    return None


def runs_cases(fn: ast.FunctionDef) -> bool:
    """Whether fn is parametrized over cases."""
    return any(
        is_call(d, "pytest.mark.parametrize") and literal(d.args[0]) == "case"
        for d in fn.decorator_list
        if isinstance(d, ast.Call) and d.args
    )


def check_tables(tree: ast.Module) -> list[Violation]:
    """Each table is a list, run by the test directly after it, and no other test runs cases."""
    out = []
    for i, stmt in enumerate(tree.body):
        before = tree.body[i - 1] if i > 0 else None
        if isinstance(stmt, ast.FunctionDef) and runs_cases(stmt) and (before is None or table_name(before) is None):
            msg = f"{stmt.name} runs cases but doesn't follow the <SUBJECT>_CASES table it runs"
            out.append(Violation(stmt.lineno, "MPT103", msg))

        if not isinstance(stmt, ast.Assign | ast.AnnAssign) or (table := table_name(stmt)) is None:
            continue
        if not isinstance(stmt.value, ast.List):
            out.append(Violation(stmt.lineno, "MPT104", f"{table} is built by code, not written as a list of cases"))
        test = "test_" + table.removesuffix("_CASES").lower()
        after = tree.body[i + 1] if i + 1 < len(tree.body) else None
        if not isinstance(after, ast.FunctionDef) or after.name != test:
            out.append(Violation(stmt.lineno, "MPT101", f"{table} isn't followed directly by {test}"))
            continue
        want = f'pytest.mark.parametrize("case", {table}, ids=lambda case: case.name)'
        dumped = ast.dump(ast.parse(want, mode="eval").body)
        if not any(ast.dump(d) == dumped for d in after.decorator_list):
            out.append(Violation(after.lineno, "MPT102", f"{test} isn't decorated with @{want}"))
    return out


def check_case_classes(tree: ast.Module) -> list[Violation]:
    """Each case dataclass's fields start with name and reason, and end with want."""
    out = []
    for stmt in tree.body:
        if not isinstance(stmt, ast.ClassDef) or not stmt.name.endswith("Case"):
            continue
        fields = [
            f"{ast.unparse(s.target)}: {ast.unparse(s.annotation)}" for s in stmt.body if isinstance(s, ast.AnnAssign)
        ]
        if fields[:2] != ["name: str", "reason: str"] or not fields[2:] or not fields[-1].startswith("want:"):
            msg = f"{stmt.name}'s fields don't start name: str, reason: str and end want"
            out.append(Violation(stmt.lineno, "MPT201", msg))
    return out


def check_cases(tree: ast.Module) -> list[Violation]:
    """Each case passes a short, unique CamelCase name and a one-sentence reason, as literals."""
    out = []
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign | ast.AnnAssign) or (table := table_name(stmt)) is None:
            continue
        if not isinstance(stmt.value, ast.List):
            continue
        seen = set()
        for entry in stmt.value.elts:
            kwargs = {k.arg: k.value for k in entry.keywords} if isinstance(entry, ast.Call) else {}
            name, reason = literal(kwargs.get("name")), literal(kwargs.get("reason"))
            if name is None or reason is None:
                msg = "a case doesn't pass name= and reason= as string literals"
                out.append(Violation(entry.lineno, "MPT202", msg))
                continue
            line = kwargs["name"].lineno
            if not CAMEL_CASE.fullmatch(name) or len(WORD.findall(name)) > MAX_NAME_WORDS:
                out.append(Violation(line, "MPT203", f"{name!r} isn't CamelCase of at most four words"))
            if name in seen:
                out.append(Violation(line, "MPT204", f"{name!r} names another case in {table}"))
            seen.add(name)
            if not reason.endswith(".") or SENTENCE_BREAK.search(reason):
                msg = "reason isn't one sentence ending in a full stop"
                out.append(Violation(kwargs["reason"].lineno, "MPT205", msg))
    return out


def asserts(node: ast.AST) -> collections.abc.Iterator[ast.Assert]:
    """The asserts under node, other than those in a with pytest.raises block."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.With) and any(is_call(i.context_expr, "pytest.raises") for i in child.items):
            continue
        if isinstance(child, ast.Assert):
            yield child
        yield from asserts(child)


def check_assertions(tree: ast.Module) -> list[Violation]:
    """A case test's asserts pass case.reason as their message."""
    return [
        Violation(a.lineno, "MPT301", f"{stmt.name}'s assert doesn't pass case.reason as its message")
        for stmt in tree.body
        if isinstance(stmt, ast.FunctionDef) and runs_cases(stmt)
        for a in asserts(stmt)
        if a.msg is None or ast.unparse(a.msg) != "case.reason"
    ]


def check_derivations(tree: ast.Module) -> list[Violation]:
    """Nothing copies, merges or computes a value a case should write out."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and (node.attr in DERIVING_METHODS or ast.unparse(node) in COPIES):
            out.append(Violation(node.lineno, "MPT401", f"{ast.unparse(node)} derives or mutates a value"))
        if isinstance(node, ast.ImportFrom) and node.module == "copy":
            out.append(Violation(node.lineno, "MPT401", "the copy module derives a value"))
        if (isinstance(node, ast.Attribute) and node.attr == "child_name") or (
            isinstance(node, ast.ImportFrom) and any(a.name == "child_name" for a in node.names)
        ):
            out.append(Violation(node.lineno, "MPT402", "child_name computes a name a case should write as a literal"))
    return out


def immutable(node: ast.expr) -> bool:
    """Whether node is a constant, a tuple of constants, an alias, or a type such as Literal[...]."""
    if isinstance(node, ast.Tuple):
        return all(immutable(e) for e in node.elts)
    if isinstance(node, ast.BinOp):
        return immutable(node.left) and immutable(node.right)
    return isinstance(node, ast.Constant | ast.JoinedStr | ast.Subscript | ast.Name | ast.Attribute)


def check_globals(tree: ast.Module) -> list[Violation]:
    """The tables are the only module-level objects that aren't constants."""
    out = []
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign | ast.AnnAssign) or stmt.value is None or table_name(stmt) is not None:
            continue
        if not immutable(stmt.value):
            target = ast.unparse(stmt.targets[0] if isinstance(stmt, ast.Assign) else stmt.target)
            out.append(Violation(stmt.lineno, "MPT403", f"module-level {target} isn't a table or a constant"))
    return out


def check_helpers(tree: ast.Module) -> list[Violation]:
    """A helper takes only keyword-only parameters, with no defaults."""
    out = []
    for stmt in tree.body:
        if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) or not stmt.name.startswith("_"):
            continue
        if stmt.name == "_to_dict":
            continue
        args = stmt.args
        params = [a.arg for a in [*args.posonlyargs, *args.args]]
        params += [f"*{args.vararg.arg}"] if args.vararg else []
        params += [f"**{args.kwarg.arg}"] if args.kwarg else []
        params += [f"{a.arg}=" for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None]
        if params:
            msg = f"{stmt.name} takes {', '.join(params)}; a helper's parameters are keyword-only with no defaults"
            out.append(Violation(stmt.lineno, "MPT501", msg))
    return out


def check_tests(tree: ast.Module) -> list[Violation]:
    """Tests are functions named in at most three words, with no unittest."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "unittest" for a in node.names):
            out.append(Violation(node.lineno, "MPT601", "unittest is imported"))
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "unittest":
            out.append(Violation(node.lineno, "MPT601", "unittest is imported"))
        if isinstance(node, ast.ClassDef) and any(ast.unparse(b).split(".")[-1] == "TestCase" for b in node.bases):
            out.append(Violation(node.lineno, "MPT602", f"{node.name} derives from TestCase"))
    for stmt in tree.body:
        if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) or not stmt.name.startswith("test_"):
            continue
        if len(stmt.name.removeprefix("test_").split("_")) > MAX_TEST_WORDS:
            out.append(Violation(stmt.lineno, "MPT603", f"{stmt.name} has more than three words after test_"))
    return out


def escapes(source: str) -> tuple[dict[int, set[str]], list[Violation]]:
    """The codes each line escapes, and the escapes that state no reason."""
    escaped: dict[int, set[str]] = {}
    unexplained = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT or (match := NOQA.search(tok.string)) is None:
            continue
        codes = {c for c in re.split(r"[\s,]+", match["codes"]) if c.startswith("MPT")}
        if not codes:
            continue
        escaped[tok.start[0]] = codes
        if not match["reason"].strip(" #-:"):
            msg = "escape states no reason; write # noqa: MPTnnn  # why"
            unexplained.append(Violation(tok.start[0], "MPT001", msg))
    return escaped, unexplained


def check(source: str) -> list[Violation]:
    """The rules source breaks, other than those it escapes."""
    tree = ast.parse(source)
    found = [
        *check_tables(tree),
        *check_case_classes(tree),
        *check_cases(tree),
        *check_assertions(tree),
        *check_derivations(tree),
        *check_globals(tree),
        *check_helpers(tree),
        *check_tests(tree),
    ]
    escaped, unexplained = escapes(source)
    return sorted([v for v in found if v.code not in escaped.get(v.line, set())] + unexplained)


def main() -> int:
    """Check the files named on the command line, or every function's tests."""
    paths = [pathlib.Path(a).resolve() for a in sys.argv[1:]] or sorted(REPO.glob("functions/*/tests/test_*.py"))
    count = 0
    for path in paths:
        shown = path.relative_to(REPO) if path.is_relative_to(REPO) else path
        for v in check(path.read_text()):
            print(f"{shown}:{v.line}: {v.code} {v.message}", file=sys.stderr)
            count += 1
    print(f"\nchecked {len(paths)} test file(s)")
    if count:
        print(f"{count} violation(s) of CONTRIBUTING.md's Tests section", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
