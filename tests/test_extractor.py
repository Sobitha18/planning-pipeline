"""13 tests for src.indexer.extractor: symbol extraction, both languages."""

from pathlib import Path

from src.indexer.extractor import extract

REPO_ROOT = Path(__file__).parent / "fixtures" / "sample_repo"
PY_FILE = REPO_ROOT / "src" / "auth" / "jwt.py"
TSX_FILE = REPO_ROOT / "src" / "components" / "dashboard.tsx"


def _extract_py():
    return extract(str(PY_FILE), PY_FILE.read_text(), repo_root=str(REPO_ROOT))


def _extract_tsx():
    return extract(str(TSX_FILE), TSX_FILE.read_text(), repo_root=str(REPO_ROOT))


def _by_qname(symbols):
    return {s.qualified_name: s for s in symbols}


# ---------------------------------------------------------------- Python


def test_py_qualified_names():
    names = {s.qualified_name for s in _extract_py()}
    assert names == {
        "auth.jwt.JwtValidator",
        "auth.jwt.JwtValidator.verify",
        "auth.jwt.JwtValidator._decode",
        "auth.jwt.JwtValidator.Inner",
        "auth.jwt.JwtValidator.Inner.ping",
        "auth.jwt.refresh_token",
        "auth.jwt.refresh_token._helper",
        "auth.jwt._module_private",
    }


def test_py_kinds():
    by_name = _by_qname(_extract_py())
    assert by_name["auth.jwt.JwtValidator"].kind == "class"
    assert by_name["auth.jwt.JwtValidator.verify"].kind == "method"
    assert by_name["auth.jwt.JwtValidator.Inner"].kind == "class"
    assert by_name["auth.jwt.refresh_token"].kind == "function"
    assert by_name["auth.jwt.refresh_token._helper"].kind == "function"


def test_py_decorated_spans():
    by_name = _by_qname(_extract_py())
    decode = by_name["auth.jwt.JwtValidator._decode"]
    assert decode.signature.startswith("@functools.lru_cache")

    refresh = by_name["auth.jwt.refresh_token"]
    assert "@functools.wraps(print)" in refresh.signature
    assert "async def refresh_token(" in refresh.signature
    assert refresh.signature.endswith("-> str:")


def test_py_docstrings():
    by_name = _by_qname(_extract_py())
    assert by_name["auth.jwt.JwtValidator.verify"].docstring == \
        "Validates JWT and returns claims."
    assert by_name["auth.jwt.JwtValidator._decode"].docstring is None


def test_py_spans_valid():
    for s in _extract_py():
        assert 1 <= s.start_line <= s.end_line


def test_py_broken_syntax_does_not_raise():
    symbols = extract("x/y.py", "def ok():\n    pass\n\ndef broken(:\n")
    names = {s.name for s in symbols}
    assert "ok" in names


# ---------------------------------------------------------------- TypeScript


def test_tsx_qualified_names():
    names = {s.qualified_name for s in _extract_tsx()}
    expected = {
        "components.dashboard.ButtonProps",
        "components.dashboard.UserId",
        "components.dashboard.userSchema",
        "components.dashboard.buttonVariants",
        "components.dashboard.Button",
        "components.dashboard.Button.handleClick",
        "components.dashboard.createUser",
        "components.dashboard.DashboardPage",
        "components.dashboard.ApiClient",
        "components.dashboard.ApiClient.get",
        "components.dashboard.Role",
    }
    assert expected <= names


def test_tsx_kinds():
    by_name = _by_qname(_extract_tsx())
    assert by_name["components.dashboard.ButtonProps"].kind == "interface"
    assert by_name["components.dashboard.UserId"].kind == "type"
    assert by_name["components.dashboard.userSchema"].kind == "const"
    assert by_name["components.dashboard.Button"].kind == "function"
    assert by_name["components.dashboard.ApiClient"].kind == "class"
    assert by_name["components.dashboard.ApiClient.get"].kind == "method"
    assert by_name["components.dashboard.Role"].kind == "enum"


def test_tsx_exported():
    by_name = _by_qname(_extract_tsx())
    assert by_name["components.dashboard.Button"].exported is True
    assert by_name["components.dashboard.createUser"].exported is True
    assert by_name["components.dashboard.DashboardPage"].exported is True
    assert by_name["components.dashboard.ApiClient"].exported is False
    assert by_name["components.dashboard.ButtonProps"].exported is False
    assert by_name["components.dashboard.Button.handleClick"].exported is False


def test_tsx_jsdoc():
    by_name = _by_qname(_extract_tsx())
    assert "Renders a styled button." in by_name["components.dashboard.Button"].docstring
    assert by_name["components.dashboard.ButtonProps"].docstring == \
        "Props for the button component."


def test_tsx_signatures():
    by_name = _by_qname(_extract_tsx())
    assert by_name["components.dashboard.createUser"].signature == \
        "export async function createUser(input: z.infer<typeof userSchema>)"
    assert by_name["components.dashboard.Button"].signature.startswith(
        "export const Button = (")


def test_tsx_unsupported_extension():
    assert extract("styles/globals.css", "body{}") == []


def test_tsx_broken_syntax_does_not_raise():
    symbols = extract(
        "x/y.tsx",
        "export function ok() { return 1 }\nconst broken = ((( \n",
    )
    names = {s.name for s in symbols}
    assert "ok" in names


def test_tsx_local_const_not_extracted():
    """const local = useRouter() inside Button is a local, not a symbol."""
    names = {s.qualified_name for s in _extract_tsx()}
    assert "components.dashboard.Button.local" not in names


def test_tsx_file_header_jsdoc_not_attached():
    """A file-header JSDoc above the imports doesn't leak onto userSchema."""
    by_name = _by_qname(_extract_tsx())
    assert by_name["components.dashboard.userSchema"].docstring is None
