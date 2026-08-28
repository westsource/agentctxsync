"""Local dev launcher: load server/.env into the environment, then run uvicorn."""
import os
import re

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
for line in open(_ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ[k] = v

import uvicorn

if __name__ == "__main__":
    import db
    db.init_db()
    print("Backend: PostgreSQL (multi-tenant)")
    print("Web UI: http://0.0.0.0:8765/web/")
    uvicorn.run("main:app", host="0.0.0.0", port=8765, log_level="info")
