"""Tables as index symbols: SQL, migrations, ORM models. Pure extractor tests,
no DB."""

from src.indexer.extractor import extract
from src.indexer.tables import is_migration_path


def kinds(path, content):
    return [(s.kind, s.name) for s in extract(path, content)]


def test_sql_create_alter_and_index():
    sql = (
        'CREATE TABLE IF NOT EXISTS public."orders" (\n  id int\n);\n'
        "ALTER TABLE orders ADD COLUMN note text;\n"
        "CREATE UNIQUE INDEX ix ON orders (id);\n"
        "DROP TABLE legacy;\n"
    )
    assert kinds("db/schema.sql", sql) == [
        ("table", "orders"),
        ("table_change", "orders"),
        ("table_change", "orders"),
        ("table_change", "legacy"),
    ]


def test_sql_symbol_spans_and_signature():
    sql = "-- header\nCREATE TABLE users (\n  id int\n);\n"
    (sym,) = extract("schema.sql", sql)
    assert (sym.start_line, sym.end_line) == (2, 4)
    assert sym.signature.startswith("CREATE TABLE users")


def test_sqlalchemy_model_gets_table_symbol_alongside_class():
    src = 'class Order(Base):\n    __tablename__ = "orders"\n    id = 1\n'
    assert kinds("app/models.py", src) == [("class", "Order"), ("table", "orders")]


def test_python_class_without_table_binding_is_not_a_table():
    assert kinds("app/x.py", "class Helper:\n    pass\n") == [("class", "Helper")]


def test_django_model_uses_db_table_or_class_name():
    src = (
        "class Invoice(models.Model):\n    class Meta:\n        db_table = 'billing_invoice'\n\n"
        "class Plain(models.Model):\n    x = 1\n\n"
        "class Base(models.Model):\n    class Meta:\n        abstract = True\n"
    )
    tables = [n for k, n in kinds("billing/models.py", src) if k == "table"]
    assert tables == ["billing_invoice", "Plain"]


def test_typeorm_entity_with_and_without_name():
    src = '@Entity("orders")\nexport class Order {}\n\n@Entity()\nexport class Customer {}\n'
    tables = [n for k, n in kinds("src/entities.ts", src) if k == "table"]
    assert tables == ["orders", "Customer"]


def test_alembic_migration_reduced_to_table_symbols():
    src = (
        "def helper():\n    pass\n\n"
        "def upgrade():\n"
        '    op.create_table("users", sa.Column("id"))\n'
        '    op.add_column("orders", sa.Column("note"))\n'
        '    op.create_index("ix_o", "orders", ["id"])\n'
    )
    got = kinds("alembic/versions/0001_init.py", src)
    assert got == [("table", "users"), ("table_change", "orders"), ("table_change", "orders")]
    assert all(k != "function" for k, _ in got)  # boilerplate dropped


def test_django_and_knex_migrations():
    django = (
        "operations = [\n"
        "    migrations.CreateModel(name='Order', fields=[]),\n"
        "    migrations.AddField(model_name='order', name='note'),\n"
        "]\n"
    )
    assert kinds("shop/migrations/0002_x.py", django) == [("table", "Order"), ("table_change", "order")]
    knex = "exports.up = k => k.schema.createTable('orders', t => {}).alterTable('users', t => {});\n"
    assert kinds("db/migrations/20240101_x.js", knex) == [("table", "orders"), ("table_change", "users")]


def test_raw_sql_inside_a_migration_file_is_found():
    src = 'await queryRunner.query(`ALTER TABLE "orders" ADD "note" text`);\n'
    assert kinds("src/migrations/1700000-add-note.ts", src) == [("table_change", "orders")]


def test_is_migration_path():
    assert is_migration_path("db/migrations/001.sql")
    assert is_migration_path("alembic/versions/a.py")
    assert not is_migration_path("src/migrations.py")  # a file, not a directory
    assert not is_migration_path("src/orders/routes.py")
