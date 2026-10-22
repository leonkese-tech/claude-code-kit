# -*- coding: utf-8 -*-
"""code_map - find where things are in big Python files without reading them.

    python code_map.py --file big_module.py          # skeleton with line ranges
    python code_map.py --grep retry src tools        # which defs mention "retry"
    python code_map.py --file a.py --lines 120-180   # print just that range

The skeleton is typically 1-3% of the file. Read the skeleton, then read only
the line range you need. Token counts are estimates (chars / 3.2).

Stdlib only. Files that fail to parse are reported, never fatal.
"""
import argparse
import ast
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CHARS_PER_TOKEN = 3.2
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "build", "dist", ".tox"}


def est_tokens(text):
    return int(len(text) / CHARS_PER_TOKEN + 0.5)


def read_source(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def _first_line(doc):
    if not doc:
        return ""
    for ln in doc.strip().splitlines():
        if ln.strip():
            return ln.strip()
    return ""


def _signature(node):
    a = node.args
    names = [x.arg for x in getattr(a, "posonlyargs", [])] + [x.arg for x in a.args]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return "(" + ", ".join(names) + ")"


def _end(node):
    return getattr(node, "end_lineno", None) or node.lineno


def outline(tree):
    """Return a list of entries: dict(kind, name, start, end, depth, sig, doc)."""
    out = []

    def walk(body, depth, prefix):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async def" if isinstance(n, ast.AsyncFunctionDef) else "def"
                out.append(dict(kind=kind, name=prefix + n.name, start=n.lineno, end=_end(n),
                                depth=depth, sig=_signature(n), doc=_first_line(ast.get_docstring(n))))
            elif isinstance(n, ast.ClassDef):
                out.append(dict(kind="class", name=prefix + n.name, start=n.lineno, end=_end(n),
                                depth=depth, sig="", doc=_first_line(ast.get_docstring(n))))
                walk(n.body, depth + 1, "")
            elif depth == 0 and isinstance(n, (ast.Assign, ast.AnnAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id.isupper():
                        out.append(dict(kind="const", name=t.id, start=n.lineno, end=_end(n),
                                        depth=0, sig="", doc=""))
    walk(tree.body, 0, "")
    return out


def skeleton(path, src=None):
    """Return (text, error). Text is the skeleton; error is None or a message."""
    src = read_source(path) if src is None else src
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        return "", "syntax error line %s: %s" % (e.lineno, e.msg)
    lines = ["# %s (%d lines)" % (path, src.count("\n") + 1)]
    mod = _first_line(ast.get_docstring(tree))
    if mod:
        lines.append('"""%s"""' % mod)
    for e in outline(tree):
        ind = "  " * e["depth"]
        rng = "%d-%d" % (e["start"], e["end"]) if e["end"] != e["start"] else str(e["start"])
        head = "%s%s %s%s" % (ind, e["kind"], e["name"], e["sig"])
        if e["kind"] == "const":
            head = "%s%s" % (ind, e["name"])
        tail = "  # " + e["doc"][:70] if e["doc"] else ""
        lines.append("%-60s [%s]%s" % (head, rng, tail))
    return "\n".join(lines), None


def iter_py_files(paths):
    for p in paths:
        if os.path.isfile(p):
            yield p
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for f in sorted(files):
                if f.endswith(".py"):
                    yield os.path.join(root, f)


def grep(word, paths, ignore_case=True):
    """Yield (path, line_no, def_name_or_None, text) for each matching line.
    def_name is the innermost enclosing def/class, so you know which range to read."""
    flags = re.IGNORECASE if ignore_case else 0
    rx = re.compile(re.escape(word), flags)
    for path in iter_py_files(paths):
        try:
            src = read_source(path)
        except OSError:
            continue
        if not rx.search(src):
            continue
        try:
            ents = [e for e in outline(ast.parse(src)) if e["kind"] != "const"]
        except SyntaxError:
            ents = []
        for i, line in enumerate(src.splitlines(), 1):
            if rx.search(line):
                best = None
                for e in ents:
                    if e["start"] <= i <= e["end"] and (best is None or e["start"] >= best["start"]):
                        best = e
                yield path, i, best, line.strip()


def cmd_file(path, lines_range=None):
    src = read_source(path)
    if lines_range:
        a, b = lines_range
        sel = src.splitlines()[a - 1:b]
        for n, ln in enumerate(sel, a):
            print("%5d  %s" % (n, ln))
        print("\n[range %d-%d: ~%d tokens vs full file ~%d]" % (a, b, est_tokens("\n".join(sel)), est_tokens(src)))
        return 0
    text, err = skeleton(path, src)
    if err:
        print("cannot parse %s: %s" % (path, err))
        return 2
    print(text)
    full, sk = est_tokens(src), est_tokens(text)
    print("\n[tokens: full file ~%d, skeleton ~%d (%.1f%%)]" % (full, sk, 100.0 * sk / max(1, full)))
    return 0


def cmd_grep(word, paths, limit):
    hits = {}
    for path, ln, ent, text in grep(word, paths or ["."]):
        hits.setdefault((path, ent["name"] if ent else "<module level>",
                         (ent["start"], ent["end"]) if ent else None), []).append((ln, text))
    if not hits:
        print("no matches for %r" % word)
        return 1
    shown = 0
    for (path, name, rng), items in sorted(hits.items(), key=lambda kv: (kv[0][0], kv[0][2] or (0, 0))):
        if shown >= limit:
            print("... %d more groups (raise --limit)" % (len(hits) - shown))
            break
        where = "%d-%d" % rng if rng else "-"
        print("%s  %s  [%s]  %d hit(s), first line %d: %s"
              % (path, name, where, len(items), items[0][0], items[0][1][:80]))
        shown += 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Token-saving navigator for Python files.")
    ap.add_argument("--file", help="print a skeleton of this file")
    ap.add_argument("--lines", help="with --file: print only this range, e.g. 120-180")
    ap.add_argument("--grep", metavar="WORD", help="list defs containing WORD")
    ap.add_argument("paths", nargs="*", help="files/dirs for --grep (default: .)")
    ap.add_argument("--limit", type=int, default=60)
    a = ap.parse_args(argv)
    if a.file:
        rng = None
        if a.lines:
            m = re.fullmatch(r"(\d+)-(\d+)", a.lines)
            if not m:
                ap.error("--lines must look like 120-180")
            rng = (int(m.group(1)), int(m.group(2)))
        return cmd_file(a.file, rng)
    if a.grep:
        return cmd_grep(a.grep, a.paths, a.limit)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
