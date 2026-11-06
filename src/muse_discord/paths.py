"""Locate owner configuration and state outside the source package."""
from pathlib import Path


def project_root(filename):
    # All package modules, including subpackages, share src/ as their anchor.
    return next(parent.parent for parent in Path(filename).resolve().parents
                if parent.name == "src")
