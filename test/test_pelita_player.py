import pytest

from pathlib import Path
import py_compile
import sys
import tempfile

import pelita
from pelita import libpelita
from pelita.scripts.pelita_player import load_factory, load_team

SIMPLE_MODULE = """
from pelita.player import SimpleTeam, StoppingPlayer
def team():
    return SimpleTeam("%s", StoppingPlayer, StoppingPlayer)
"""

SIMPLE_FAILING_MODULE = """
def noteam():
    return None
"""

# TODO: The modules should be unloaded after use

class TestLoadFactory:
    def test_simple_module_import(self):
        modules_before = list(sys.modules.keys())
        with tempfile.TemporaryDirectory() as d:
            module = Path(d) / "teamx"
            module.mkdir()
            initfile = module / "__init__.py"
            with initfile.open(mode='w') as f:
                f.write(SIMPLE_MODULE)

            spec = str(module)
            load_factory(spec)

    def test_simple_file_import(self):
        modules_before = list(sys.modules.keys())
        with tempfile.TemporaryDirectory() as d:
            module = Path(d) / "teamy"
            module.mkdir()
            initfile = module / "teamyy.py"
            with initfile.open(mode='w') as f:
                f.write(SIMPLE_MODULE)

            spec = str(initfile)
            load_factory(spec)

    def test_failing_import(self):
        modules_before = list(sys.modules.keys())
        with tempfile.TemporaryDirectory() as d:
            module = Path(d) / "teamz"
            module.mkdir()
            initfile = module / "__init__.py"
            with initfile.open(mode='w') as f:
                f.write(SIMPLE_FAILING_MODULE)

            spec = str(module)
            with pytest.raises(AttributeError):
                load_factory(spec)

    def test_import_of_pyc(self):
        with tempfile.TemporaryDirectory() as d:
            module = Path(d) / "teampyc"
            module.mkdir()
            initfile = module / "teampycpyc.py"
            with initfile.open(mode='w') as f:
                f.write(SIMPLE_MODULE)
            pycfile = initfile.parent / "teampycpyc.pyc"
            py_compile.compile(str(initfile), cfile=str(pycfile))
            initfile.unlink()

            spec = str(pycfile)
            load_factory(spec)

class TestLoadTeam:
    def test_simple_module_import_forbidden_names(self):
        names = ["", " ", "-", "∂", "0" * 26]
        for idx, name in enumerate(names):
            modules_before = list(sys.modules.keys())
            with tempfile.TemporaryDirectory() as d:
                module = Path(d) / ("teamx_%i" % idx)
                module.mkdir()
                initfile = module / "__init__.py"
                with initfile.open(mode='w') as f:
                    f.write(SIMPLE_MODULE % (name,))

                spec = str(module)
                with pytest.raises(ValueError):
                    load_team(spec)

    def test_simple_module_import_allowed_names(self):
        names = ["a", "a a", "0" * 25]
        for idx, name in enumerate(names):
            modules_before = list(sys.modules.keys())
            with tempfile.TemporaryDirectory() as d:
                module = Path(d) / ("teamy_%i" % idx)
                module.mkdir()
                initfile = module / "__init__.py"
                with initfile.open(mode='w') as f:
                    f.write(SIMPLE_MODULE % (name,))

                spec = str(module)
                load_team(spec)

    load_team_cases = [
        ("pelita/player/StoppingPlayer", None),
#        ("StoppingPlayer,StoppingPlayer", None),
        ("NonExistingPlayer", ImportError),
#        ("StoppingPlayer,StoppingPlayer,FoodEatingPlayer", ValueError),
        ('doc/source/groupN:team', None),
        ('doc/source/groupN/__init__.py', ImportError),
        ('doc/source/groupN', ValueError), # Has already been imported
    ]

    def test_load_team(self):
        for path, result in self.load_team_cases:
            print(path, result)
            if result is not None:
                with pytest.raises(result):
                    load_team(path)
            else:
                load_team(path)
 