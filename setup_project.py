# file: setup_project.py
import os
from pathlib import Path

def setup_dirs(root_dir="gprotab"):
    root = Path(root_dir)
    directories = [
        root / "metadata",
        root / "music_data",
        root / "error_pages",
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        print(f"Initialized directory: {directory}")

if __name__ == "__main__":
    setup_dirs()