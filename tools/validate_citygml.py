"""Shim: the code is src/pykci/validate_citygml.py (see _pykci_shim.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _pykci_shim import forward  # noqa: E402

forward(__name__, __file__)
