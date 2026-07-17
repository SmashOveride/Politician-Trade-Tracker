"""
Packaged-app entry point used by the PyInstaller build (see build specs in
packaging/). Identical to run.py -- this is just the module PyInstaller
freezes into the standalone executable that end users double-click.
"""

from backend.launcher import launch

if __name__ == "__main__":
    launch()
