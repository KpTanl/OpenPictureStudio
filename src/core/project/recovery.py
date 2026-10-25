from pathlib import Path
def has_pending_journal(root: str) -> bool:
    p=Path(root)/"journal"
    return p.exists() and any(p.iterdir())
