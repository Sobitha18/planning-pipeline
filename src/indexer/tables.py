"""Database tables as first-class index symbols.

A feature can need a schema change, and the repo that owns that schema (and the
repos that read the affected tables) have to be findable. So every place a
table is defined or changed becomes a Symbol, language-agnostically:

- kind "table"        a table is defined: SQL `CREATE TABLE`, an ORM class bound
                      to a table (SQLAlchemy `__tablename__`, Django model,
                      TypeORM `@Entity`, sequelize-typescript `@Table`), or a
                      migration that creates one (alembic/knex/Django/TypeORM).
- kind "table_change" a migration alters/drops/indexes an existing table.

Prisma models keep their own kind "model" (see prisma.py); TABLE_KINDS is the
set a "show me the tables" query should match.

Regex/line based like prisma.py: tolerant of dialect differences, never raises,
and a miss just means one less symbol. Migration files are reduced to ONLY
these symbols (their upgrade()/downgrade() boilerplate is not worth indexing).
"""

from __future__ import annotations

import re
from pathlib import Path

from src.indexer.extractor import Symbol, _module_path

TABLE_KINDS = ("table", "table_change", "model")

MIGRATION_DIRS = {"migrations", "migration", "migrate", "alembic"}

# identifier, optionally quoted ("x", `x`, [x]) and optionally schema-qualified
_IDENT = r'(?:[`"\[]?[\w$]+[`"\]]?\.)?[`"\[]?([\w$]+)[`"\]]?'

_SQL_CREATE = re.compile(
    rf"\bCREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP(?:ORARY)?\s+|UNLOGGED\s+)?TABLE\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?{_IDENT}", re.IGNORECASE)
_SQL_CHANGE = re.compile(
    rf"\b(?:ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?|DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?"
    rf"|CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?[\w$]+[`\"\]]?\s+ON\s+(?:ONLY\s+)?"
    rf"|RENAME\s+TABLE\s+|TRUNCATE\s+(?:TABLE\s+)?){_IDENT}", re.IGNORECASE)

_ALEMBIC = re.compile(
    r"\bop\.(create_table|drop_table|rename_table|add_column|drop_column|alter_column|"
    r"batch_alter_table|create_foreign_key|create_unique_constraint|drop_constraint)"
    r"\(\s*['\"]([\w$]+)['\"]")
_ALEMBIC_INDEX = re.compile(r"\bop\.(?:create_index|drop_index)\(\s*[^,]+,\s*['\"]([\w$]+)['\"]")
_DJANGO_CREATE = re.compile(r"\bmigrations\.CreateModel\(\s*name\s*=\s*['\"](\w+)['\"]")
_DJANGO_CHANGE = re.compile(
    r"\bmigrations\.(?:AddField|RemoveField|AlterField|RenameField|AlterModelTable|"
    r"AddIndex|RemoveIndex|AlterUniqueTogether)\(\s*model_name\s*=\s*['\"](\w+)['\"]|"
    r"\bmigrations\.(?:DeleteModel|RenameModel)\(\s*(?:old_)?name\s*=\s*['\"](\w+)['\"]")
_KNEX = re.compile(r"\.(createTable|createTableIfNotExists|alterTable|dropTable|dropTableIfExists|table)"
                   r"\(\s*['\"`]([\w$]+)['\"`]")
_TYPEORM_TABLE = re.compile(r"new\s+Table\(\s*\{\s*name:\s*['\"`]([\w$]+)['\"`]")

_PY_TABLENAME = re.compile(r"__tablename__\s*=\s*['\"]([\w.$]+)['\"]")
_DJANGO_BASE = re.compile(r"\b(?:models\.)?Model\b")
_DJANGO_DB_TABLE = re.compile(r"db_table\s*=\s*['\"]([\w.$]+)['\"]")
_ENTITY = re.compile(
    r"@(?:Entity|Table)\(\s*(?:['\"`]([\w.$]+)['\"`]|\{[^}]*?(?:name|tableName):\s*['\"`]([\w.$]+)['\"`])?")


def is_migration_path(file_path: str) -> bool:
    return any(part in MIGRATION_DIRS for part in Path(file_path).parts[:-1])


def _lineno(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _statement_end(text: str, start: int) -> int:
    end = text.find(";", start)
    return len(text) if end == -1 else end + 1


def _sym(kind: str, table: str, module: str, language: str, text: str,
         start: int, end: int, exported: bool = True) -> Symbol:
    snippet = " ".join(text[start:end].split())
    return Symbol(
        kind=kind, name=table, qualified_name=f"{module}.{table}" if module else table,
        start_line=_lineno(text, start), end_line=_lineno(text, max(start, end - 1)),
        signature=snippet if len(snippet) <= 500 else snippet[:500] + "…",
        docstring=None, exported=exported, language=language,
    )


def sql_table_symbols(text: str, module: str, language: str) -> list[Symbol]:
    out: list[Symbol] = []
    for m in _SQL_CREATE.finditer(text):
        out.append(_sym("table", m.group(1), module, language, text, m.start(), _statement_end(text, m.start())))
    for m in _SQL_CHANGE.finditer(text):
        out.append(_sym("table_change", m.group(1), module, language, text, m.start(), _statement_end(text, m.start())))
    return out


def migration_call_symbols(text: str, module: str, language: str) -> list[Symbol]:
    """Table symbols from migration-framework calls (any language) plus any
    raw SQL embedded in the file."""
    out: list[Symbol] = []

    def line_sym(kind: str, table: str, m: re.Match) -> None:
        line_end = text.find("\n", m.start())
        out.append(_sym(kind, table, module, language, text, m.start(),
                        len(text) if line_end == -1 else line_end))

    for m in _ALEMBIC.finditer(text):
        line_sym("table" if m.group(1) == "create_table" else "table_change", m.group(2), m)
    for m in _ALEMBIC_INDEX.finditer(text):
        line_sym("table_change", m.group(1), m)
    for m in _DJANGO_CREATE.finditer(text):
        line_sym("table", m.group(1), m)
    for m in _DJANGO_CHANGE.finditer(text):
        line_sym("table_change", m.group(1) or m.group(2), m)
    for m in _KNEX.finditer(text):
        line_sym("table" if m.group(1).startswith("createTable") else "table_change", m.group(2), m)
    for m in _TYPEORM_TABLE.finditer(text):
        line_sym("table", m.group(1), m)
    out.extend(sql_table_symbols(text, module, language))
    return out


def orm_table_symbols(class_symbols: list[Symbol], source: str, module: str, language: str) -> list[Symbol]:
    """One "table" symbol for each class bound to a table. `class_symbols` are
    the extractor's kind=="class" symbols; spans come from them."""
    lines = source.splitlines()
    out: list[Symbol] = []
    for cls in class_symbols:
        body = "\n".join(lines[cls.start_line - 1 : cls.end_line])
        table: str | None = None
        if language == "python":
            if (m := _PY_TABLENAME.search(body)):
                table = m.group(1)
            elif _DJANGO_BASE.search(cls.signature) and "abstract = True" not in body:
                m = _DJANGO_DB_TABLE.search(body)
                table = m.group(1) if m else cls.name
        else:
            m = _ENTITY.search(body)
            if m:
                table = m.group(1) or m.group(2) or cls.name
        if table:
            out.append(Symbol(
                kind="table", name=table,
                qualified_name=f"{module}.{table}" if module else table,
                start_line=cls.start_line, end_line=cls.end_line,
                signature=cls.signature, docstring=cls.docstring,
                exported=True, language=language,
            ))
    return out


def sql_file_symbols(file_path: str, content: str | bytes, repo_root: str | None = None) -> list[Symbol]:
    if isinstance(content, bytes):
        content = content.decode(errors="replace")
    return sql_table_symbols(content, _module_path(file_path, repo_root), "sql")
