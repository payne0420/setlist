#!/usr/bin/env python
"""Detect duplicate same-name function/method definitions (silent shadowing).

The exact failure mode git 3-way merges create: both sides insert a method
with the same name at different points -> no conflict marker, later def wins.
Run it after EVERY merge or large agent-authored change.

Usage: python scripts/dup_method_check.py FILE [FILE...]
"""

import ast
import sys


def scan(node, scope, path):
    found = 0
    seen = {}
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if child.name in seen:
                print(
                    f"{path}:{child.lineno}: DUPLICATE def {scope}{child.name} "
                    f"(first at line {seen[child.name]})"
                )
                found += 1
            else:
                seen[child.name] = child.lineno
        if isinstance(child, ast.ClassDef):
            found += scan(child, scope + child.name + ".", path)
    return found


def main():
    bad = 0
    for path in sys.argv[1:]:
        with open(path) as fh:
            tree = ast.parse(fh.read(), filename=path)
        bad += scan(tree, "", path)
    print(
        f"{'FAIL' if bad else 'OK'}: {bad} duplicate definitions across {len(sys.argv) - 1} files"
    )
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
