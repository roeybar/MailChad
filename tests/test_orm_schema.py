"""ORM schema net: models == migrated schema, relationships,
no raw SQL outside db.py, seeder identities. Raw sqlite3 is the reference."""
from __future__ import annotations

import ast
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import orm_levels  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "mailman"


def check_bodies(sql: str) -> list[str]:
    """Whitespace-collapsed CHECK (...) bodies, extracted with balanced parentheses."""
    out = []
    for m in re.finditer(r"\bCHECK\s*\(", sql, re.I):
        depth, j = 0, m.end() - 1
        for j in range(m.end() - 1, len(sql)):
            depth += {"(": 1, ")": -1}.get(sql[j], 0)
            if depth == 0:
                break
        out.append(" ".join(sql[m.end():j].split()))
    return sorted(out)


def schema_facts(path) -> dict:
    c = sqlite3.connect(str(path))
    try:
        facts = {}
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for t in tables:
            sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
            idx = []
            for _seq, name, unique, origin, partial in c.execute(f"PRAGMA index_list('{t}')"):
                cols = tuple((x[2], x[3], x[4]) for x in c.execute(f"PRAGMA index_xinfo('{name}')") if x[5])
                idx.append((name if origin == "c" else origin, unique, partial, cols))
            facts[t] = {
                "xinfo": [tuple(r[1:6]) for r in c.execute(f"PRAGMA table_xinfo('{t}')")],
                "fk": sorted(tuple(r[2:7]) for r in c.execute(f"PRAGMA foreign_key_list('{t}')")),
                "idx": sorted(idx, key=repr),
                "autoincrement": "AUTOINCREMENT" in sql.upper(),
                "checks": check_bodies(sql),
            }
        return facts
    finally:
        c.close()


def create_all_db(models, path) -> None:
    from sqlalchemy import create_engine
    eng = create_engine(f"sqlite:///{path}")
    models.Base.metadata.create_all(eng)
    eng.dispose()


def _migrated_tables(kind: str) -> list[str]:
    import tempfile
    p = Path(tempfile.mkdtemp(prefix="orm-tables-")) / "t.sqlite"
    orm_levels.make_db_at_level(kind, p, len(orm_levels.migration_files(kind)), seed=False)
    return sorted(schema_facts(p))


def _models_equal_schema(kind: str, table: str, tmp_path, monkeypatch) -> None:
    import importlib
    db = importlib.import_module(f"mailman.{kind}.db")
    models = importlib.import_module(f"mailman.{kind}.models")
    migrated, created = tmp_path / "migrated.sqlite", tmp_path / "created.sqlite"
    monkeypatch.setattr(db, "DB_PATH", migrated)
    db.init()
    create_all_db(models, created)
    a, b = schema_facts(migrated), schema_facts(created)
    if table == "__table_names__":
        assert sorted(a) == sorted(b)
    else:
        assert a[table] == b.get(table), table


@pytest.mark.parametrize("table", _migrated_tables("terminal") + ["__table_names__"])
def test_terminal_models_equal_migrated_schema(table, tmp_path, monkeypatch):
    _models_equal_schema("terminal", table, tmp_path, monkeypatch)


@pytest.mark.parametrize("table", _migrated_tables("cloud") + ["__table_names__"])
def test_cloud_models_equal_migrated_schema(table, tmp_path, monkeypatch):
    _models_equal_schema("cloud", table, tmp_path, monkeypatch)


def check_relationships(models, path) -> None:
    """Each FK row has one many-to-one relationship on the child and one
    one-to-many on the parent; both navigate to the raw rows."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    eng = create_engine(f"sqlite:///{path}")
    raw = sqlite3.connect(str(path))
    mappers = {m.local_table.name: m for m in models.Base.registry.mappers}
    n_fk = 0
    try:
        with Session(eng) as s:
            for child, mapper in mappers.items():
                fks = list(raw.execute(f"PRAGMA foreign_key_list('{child}')"))
                n_m2o = sum(1 for r in mapper.relationships if r.direction.name == "MANYTOONE")
                assert n_m2o == len(fks), child
                for fk in fks:
                    n_fk += 1
                    parent, frm = fk[2], fk[3]
                    to = fk[4] or orm_levels.pk_columns(raw, parent)[0]
                    pair = ((child, frm), (parent, to))
                    m2o = [r for r in mapper.relationships if r.direction.name == "MANYTOONE"
                           and [((a.table.name, a.name), (b.table.name, b.name)) for a, b in r.local_remote_pairs] == [pair]]
                    o2m = [r for r in mappers[parent].relationships if r.direction.name == "ONETOMANY"
                           and [((b.table.name, b.name), (a.table.name, a.name)) for a, b in r.local_remote_pairs] == [pair]]
                    assert len(m2o) == 1 and len(o2m) == 1, (child, frm)
                    assert m2o[0].passive_deletes and o2m[0].passive_deletes, (child, frm)
                    for obj in s.scalars(select(mapper.class_)):
                        val = getattr(obj, frm)
                        target = getattr(obj, m2o[0].key)
                        assert (target is None) if val is None else getattr(target, to) == val
                    for pobj in s.scalars(select(mappers[parent].class_)):
                        pval = getattr(pobj, to)
                        cnt = raw.execute(f"SELECT count(*) FROM {child} WHERE {frm} = ?", (pval,)).fetchone()[0]
                        assert len(getattr(pobj, o2m[0].key)) == cnt, (child, frm, pval)
    finally:
        raw.close()
        eng.dispose()
    assert n_fk > 0


def test_model_relationships(tmp_path):
    import importlib
    for kind in ("terminal", "cloud"):
        models = importlib.import_module(f"mailman.{kind}.models")
        p = tmp_path / f"{kind}.sqlite"
        orm_levels.make_db_at_level(kind, p, len(orm_levels.migration_files(kind)))
        if not any(fk for fk in (schema_facts(p)[t]["fk"] for t in schema_facts(p))):
            assert all(not m.relationships for m in models.Base.registry.mappers), kind
            continue
        check_relationships(models, p)


# Raw SQL scanner -----------------------------------------------------------

_EXEC = {"execute", "executemany", "executescript", "exec_driver_sql"}


_DB_PY = {"src/mailman/terminal/db.py", "src/mailman/cloud/db.py"}   # exempt by repo-relative path


def _is_db_py(path: Path) -> bool:
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix() in _DB_PY
    except ValueError:
        return False


_MODELS_PY = {"src/mailman/terminal/models.py", "src/mailman/cloud/models.py"}   # text exemption by repo-relative path


def _is_models_py(path: Path) -> bool:
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix() in _MODELS_PY
    except ValueError:
        return False


_TEXT_PATHS = {"sqlalchemy.text", "sqlalchemy.sql.text", "sqlalchemy.sql.expression.text"}


def _aliases(tree) -> dict[str, str]:
    """Local name -> qualified sqlalchemy path, from `import x as y` and `from x import y [as z]`."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("sqlalchemy"):
                    out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("sqlalchemy"):
            for a in n.names:
                out[a.asname or a.name] = f"{n.module}.{a.name}"
    return out


def _qualified(node, aliases: dict) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(aliases.get(node.id, node.id))
    return ".".join(reversed(parts))


def _strish(node, bound: set) -> bool:
    """True when the expression produces a str: a constant, an f-string, a % or + with a str
    operand, a .format or .join call on one, str(...), a conditional with a str branch, or a name
    bound to one (or a lambda parameter)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _strish(node.left, bound) or _strish(node.right, bound)
    if isinstance(node, ast.IfExp):
        return _strish(node.body, bound) or _strish(node.orelse, bound)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "str":
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("format", "join"):
            return _strish(node.func.value, bound)
        return False
    return isinstance(node, ast.Name) and node.id in bound


def _scope_nodes(scope):
    """Nodes of one scope, not descending into nested functions or lambdas."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))


def _bind(nodes, bound: set) -> set:
    bound = set(bound)
    changed = True
    while changed:
        changed = False
        for n in nodes:
            pairs = []
            if isinstance(n, ast.Assign):
                pairs = [(t, n.value) for t in n.targets]
            elif isinstance(n, ast.AnnAssign) and n.value is not None:
                pairs = [(n.target, n.value)]
            for t, v in pairs:
                if isinstance(t, ast.Name) and t.id not in bound and _strish(v, bound):
                    bound.add(t.id)
                    changed = True
    return bound


def raw_sql_offenders(path: Path) -> list[str]:
    path = Path(path)
    if _is_db_py(path):
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    module_bound = _bind(list(_scope_nodes(tree)), set())   # module-level str constants reach functions
    scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
    for scope in scopes:
        nodes = list(_scope_nodes(scope))
        seed = set(module_bound)
        if isinstance(scope, ast.Lambda):
            seed |= {a.arg for a in scope.args.args}
        bound = _bind(nodes, seed)
        for n in nodes:
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _EXEC:
                if n.args and _strish(n.args[0], bound):
                    out.append(f"{path.name}:{n.lineno}")
    if not _is_models_py(path):
        aliases = _aliases(tree)
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("sqlalchemy") and any(
                    a.name == "text" for a in n.names):
                out.append(f"{path.name}:{n.lineno}:text-import")
            if isinstance(n, ast.Attribute) and n.attr == "text" and _qualified(n, aliases) in _TEXT_PATHS:
                out.append(f"{path.name}:{n.lineno}:text-use")
    return sorted(set(out), key=lambda s: (int(s.split(":")[1]), s))


def raw_sql_scan_files() -> list[Path]:
    files = [p for p in sorted((SRC / "terminal").rglob("*.py")) if not _is_db_py(p)]
    files += [p for p in (SRC / "cloud" / "models.py", ROOT / "scripts" / "_backup.py") if p.exists()]
    return files


SAMPLES = {   # always-on positive samples for the raw SQL scanner
    "plain.py": ('def f(c):\n    c.execute("SELECT 1")\n', ["plain.py:2"]),
    "lam.py": ("def g(c):\n    s = lambda q: c.execute(q)\n    sql = 'SELECT %s' % 1\n    c.execute(sql)\n"
               "    return s('x')\n", ["lam.py:2", "lam.py:4"]),
    "forms.py": ("Q: str = 'SELECT 1'\nTOP = 'SELECT 2'\n\n\ndef h(c, flag):\n    a: str = 'SELECT 3'\n"
                 "    c.execute(a)\n    c.execute(TOP)\n    c.execute(''.join(['SELECT', ' 4']))\n"
                 "    c.execute('A' if flag else 'B')\n    c.execute(str(flag))\n    c.execute(Q)\n"
                 "    c.execute(flag)\n",
                 ["forms.py:7", "forms.py:8", "forms.py:9", "forms.py:10", "forms.py:11", "forms.py:12"]),
    "text_forms.py": ("from sqlalchemy.sql import text as T\nimport sqlalchemy.sql.expression as E\n"
                      "from sqlalchemy import sql\nimport sqlalchemy as sa\nE.text('x')\nsql.text('y')\n"
                      "sa.sql.expression.text('z')\nsa.select(1)\n",
                      ["text_forms.py:1:text-import", "text_forms.py:5:text-use", "text_forms.py:6:text-use",
                       "text_forms.py:7:text-use"]),
}


def test_raw_sql_scanner_positive_samples(tmp_path):
    assert raw_sql_offenders(SRC / "terminal" / "db.py") == []
    assert raw_sql_offenders(SRC / "cloud" / "db.py") == []
    assert raw_sql_offenders(SRC / "cloud" / "models.py") == []
    assert raw_sql_offenders(SRC / "terminal" / "models.py") == []
    for name, (code, want) in SAMPLES.items():
        p = tmp_path / name
        p.write_text(code, encoding="ascii")
        assert raw_sql_offenders(p) == want, name
    # A module named db.py outside the two exempt paths, even under mailman/cloud/, is scanned.
    fake = tmp_path / "mailman" / "cloud" / "db.py"
    fake.parent.mkdir(parents=True)
    fake.write_text('def f(c):\n    c.execute("SELECT 1")\n', encoding="ascii")
    assert raw_sql_offenders(fake) == ["db.py:2"]
    # A module named models.py outside the two exempt paths may not import sqlalchemy.text.
    fake_models = tmp_path / "mailman" / "cloud" / "models.py"
    fake_models.write_text("from sqlalchemy import text\n", encoding="ascii")
    assert raw_sql_offenders(fake_models) == ["models.py:1:text-import"]


def test_no_raw_sql_outside_db():
    from mailman.cloud import db as cloud_db
    from mailman.terminal import db
    offenders = [o for p in raw_sql_scan_files() for o in raw_sql_offenders(p)]
    assert offenders == []
    assert not hasattr(db, "conn")
    assert not hasattr(cloud_db, "conn")


# Seeder (raw sqlite3 only) -------------------------------------------------

def test_seeder_rows_have_identities(tmp_path):
    for kind, k in [(kd, k) for kd in ("terminal", "cloud")
                    for k in range(len(orm_levels.migration_files(kd)) + 1)]:
        p = tmp_path / f"{kind}-{k}.sqlite"
        seeded = orm_levels.make_db_at_level(kind, p, k)
        c = sqlite3.connect(str(p))
        try:
            assert sorted(seeded) == sorted(orm_levels.seed_tables(c)), (kind, k)
            for t, rows in seeded.items():
                key = orm_levels.pk_columns(c, t)
                assert len(rows) == 2, (kind, k, t)
                if key:
                    ids = [tuple(r.get(col) for col in key) for r in rows]
                else:
                    ids = [c.execute(f"SELECT rowid FROM {t} WHERE " + " AND ".join(
                        f"{col} IS ?" for col in r), list(r.values())).fetchone() for r in rows]
                assert all(v is not None for i in ids for v in (i or (None,))), (kind, k, t)
                assert ids[0] != ids[1], (kind, k, t)
        finally:
            c.close()
