import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

import config
from runtime.game_logger import glog

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/sessions")
async def list_sessions():
    log_dir: Path = config.SESSION_LOG_DIR
    if not log_dir.exists():
        return {"sessions": [], "current": None}
    files = sorted(
        [f for f in log_dir.iterdir() if f.suffix == ".jsonl" and f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return {
        "sessions": [
            {
                "name": f.stem,
                "filename": f.name,
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
            }
            for f in files
        ],
        "current": glog._session_id or None,
    }


@router.get("/sessions/{filename}")
async def get_session(filename: str):
    if not filename.endswith(".jsonl"):
        filename += ".jsonl"
    log_dir: Path = config.SESSION_LOG_DIR
    path = log_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"日志文件不存在: {filename}")
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return {"entries": entries, "count": len(entries)}
