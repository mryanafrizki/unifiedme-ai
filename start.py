"""Startup script — init DB then run unified proxy."""
import os
import sys
import subprocess

DB_PATH = "unified/data/unified.db"

if __name__ == "__main__":
    os.makedirs("unified/data", exist_ok=True)
    
    # Run the proxy
    sys.exit(subprocess.call([sys.executable, "-m", "unified.main"]))
