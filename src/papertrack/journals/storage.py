"""Runtime evidence belongs to the working directory, separate from exports."""
from pathlib import Path
import json


def runs_root() -> Path:
    return Path.cwd() / '.papertrack-runs'


def cache_roots(data_dir: str | Path) -> list[Path]:
    """Read legacy exports too, but never create new evidence there."""
    return list(dict.fromkeys((runs_root(), Path(data_dir).resolve() / '.papertrack-runs')))


def move_legacy_cache(data_dir: str | Path) -> Path:
    """Move a legacy cache on the same filesystem, retaining every source byte.

    Call with collectors stopped. Refuse to merge directories or overwrite runs.
    A relocation manifest lets readers resolve old absolute paths in records.
    """
    source = Path(data_dir).resolve() / '.papertrack-runs'
    destination = runs_root()
    if source == destination or not source.exists():
        return destination
    if destination.exists():
        raise FileExistsError(f'Refusing to merge journal caches: {destination}')
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f'Expected a real cache directory: {source}')
    manifest = source / '.relocation.json'
    if manifest.exists():
        raise FileExistsError(f'Cache already has relocation history: {manifest}')
    manifest.write_text(json.dumps({'source': str(source), 'destination': str(destination)},
                                   indent=2) + '\n')
    try:
        source.rename(destination)
    except OSError:
        manifest.unlink()
        raise
    return destination


def relocated_record(record: dict, data_dir: str | Path, run_dir: Path | None = None) -> dict:
    """Resolve moved run references without rewriting original evidence or logs."""
    destination = runs_root()
    origins = [Path(data_dir).resolve() / '.papertrack-runs']
    # Older copied runs can still name an even earlier temporary cache. Bind
    # only matching run-directory identities, never arbitrary external captures.
    capture = Path(record.get('capture_path') or 'capture.json')
    if (run_dir is not None and capture.is_absolute()
            and capture.parent.name == run_dir.name
            and capture.parent.parent.name == '.papertrack-runs'):
        origins.append(capture.parent.parent)
    manifest = destination / '.relocation.json'
    if manifest.is_file():
        try:
            origin = Path(json.loads(manifest.read_text())['source'])
            if not origin.is_absolute() or origin.name != '.papertrack-runs':
                raise ValueError('Invalid original cache root')
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RuntimeError(f'Cannot resolve relocated cache: {manifest}') from error
        origins.append(origin)

    def resolve(value):
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, str) and value.startswith('/'):
            for origin in origins:
                try:
                    target = destination / Path(value).relative_to(origin)
                except ValueError:
                    continue
                if target.exists():
                    return str(target)
        return value

    return resolve(record)
