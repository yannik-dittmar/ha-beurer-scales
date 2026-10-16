"""The protocol package must stay free of Home Assistant (ADR 0001)."""

import ast
from pathlib import Path

PROTOCOL_DIR = (
    Path(__file__).parent.parent / "custom_components" / "beurer_scales" / "protocol"
)


def test_protocol_package_does_not_import_home_assistant() -> None:
    """No module in the protocol package imports homeassistant."""
    offenders = []
    for path in sorted(PROTOCOL_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] if node.level == 0 else []
                # Relative imports must not climb out of the protocol package.
                if node.level > 1:
                    names = [f"{'.' * node.level}{node.module or ''}"]
            else:
                continue
            offenders += [
                f"{path.name}: {name}"
                for name in names
                if name.split(".")[0] == "homeassistant" or name.startswith("..")
            ]
    assert offenders == []
