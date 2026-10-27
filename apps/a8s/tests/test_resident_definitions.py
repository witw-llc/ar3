import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("name,engine,bypass", [
    ("engine-devin-resident", "devin", False),
    ("engine-devin-resident-unrestricted", "devin", True),
    ("engine-muse-resident", "muse", False),
    ("engine-muse-resident-unrestricted", "muse", True),
])
def test_resident_definition_hooks(name, engine, bypass):
    path = Path(__file__).resolve().parents[1] / "definitions" / (name + ".json")
    definition = json.loads(path.read_text())
    for argv in (definition["invoke"], definition["batch"]["invoke"], definition["idle"]["invoke"]):
        assert argv[2:5] == ["engine", engine, "run"]
        assert "--resident" in argv
        assert ("--permissions=bypass" in argv or
                    ("--permissions" in argv and argv[argv.index("--permissions") + 1] == "bypass")) == bypass
        assert "--agent" in argv and "$RECIPIENT" in argv
    assert "stop" in definition["stop"]["invoke"]
    assert "status" in definition["status"]["invoke"]
