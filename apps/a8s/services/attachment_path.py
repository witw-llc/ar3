"""Safe paths for attachment bytes inside a per-message bundle directory."""
from __future__ import annotations

from pathlib import Path


def bundle_file_path(bundle_dir: Path, filename: str) -> tuple[Path | None, str]:
    """Resolve `bundle_dir / filename` when `filename` is a single path segment.

    Returns `(path, "")` or `(None, reason)`."""
    name = (filename or "").strip()
    if not name:
        return None, "missing filename"
    if name != Path(name).name:
        return None, f"filename {name!r} is not a basename"
    root = bundle_dir.resolve()
    try:
        dest = (root / name).resolve()
        dest.relative_to(root)
    except ValueError:
        return None, f"path escapes attachment bundle {root}"
    except (OSError, RuntimeError) as e:
        return None, f"cannot resolve {root / name}: {e}"
    return dest, ""
