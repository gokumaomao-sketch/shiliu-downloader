"""Run existing tests with a disposable home before app paths are imported."""
import os
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
os.chdir(root)
with tempfile.TemporaryDirectory(prefix="shiliu-tests-") as temporary:
    Path.home = classmethod(lambda cls: Path(temporary))
    import pytest
    raise SystemExit(pytest.main(sys.argv[1:]))
