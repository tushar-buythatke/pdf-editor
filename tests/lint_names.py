#!/usr/bin/env python3
"""Catch names that are used but never defined — the failure mode of deleting code by hand.

    python tests/lint_names.py server.py tests/*.py

Scope-aware enough for this codebase: module globals, function args, every local binding form
(assignment, for, with, except, comprehension, walrus, import), class bodies and nested functions.
"""
import ast
import builtins
import sys

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "self", "cls"}


def bindings(node, into):
    """Every name this scope binds locally."""
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            into.add(n.id)
        elif isinstance(n, (ast.arg,)):
            into.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            into.add(n.name)
        elif isinstance(n, ast.alias):
            into.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            into.add(n.name)
        elif isinstance(n, ast.Global):
            into.update(n.names)
    return into


def check(path):
    tree = ast.parse(open(path).read(), path)
    module = bindings(tree, set())
    problems = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]:
        args = fn.args
        local = set()
        for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            local.add(a.arg)
        for a in (args.vararg, args.kwarg):
            if a:
                local.add(a.arg)
        for stmt in (fn.body if isinstance(fn.body, list) else [fn.body]):
            bindings(stmt, local)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if n.id not in local and n.id not in module and n.id not in BUILTINS:
                    problems.append(f"{path}:{n.lineno}: '{n.id}' is used but never defined")
    return problems


if __name__ == "__main__":
    bad = [p for f in (sys.argv[1:] or ["server.py"]) for p in check(f)]
    for p in bad:
        print(p)
    print("clean" if not bad else f"{len(bad)} problem(s)")
    sys.exit(1 if bad else 0)
