"""Test setup: import pfin from this checkout and point it at an empty private directory (never the real one)."""
import sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pfin import config,sources


def isolated(**settings):
 c=config.use(config.Config(home=tempfile.mkdtemp(),**settings));sources.reset();return c

isolated()
