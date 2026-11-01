"""Effect catalog: maps the name shown on the display to its module file, and keeps the parameter
ranges read from the device (64 04 / 64 05) for the lifetime of the server process."""

from __future__ import annotations

import json
import re
from importlib import resources


def _norm(text: str) -> str:
    return re.sub(r'[^A-Z0-9]', '', text.upper())


def _load_entries() -> list[dict]:
    return json.loads(resources.files(__package__).joinpath('data/effect_files.json').read_text())['effects']


def load_file_table() -> dict[str, str]:
    """Display name -> module file name, from the bundled table."""
    return {e['name']: e['file'] for e in _load_entries()}


def load_id_table() -> dict[int, str]:
    """Effect id (as stored in patches) -> module file name, from the bundled table."""
    return {int(e['id'], 16): e['file'] for e in _load_entries() if e.get('id')}


class EffectCatalog:
    def __init__(self, file_table: dict[str, str] | None = None, id_table: dict[int, str] | None = None):
        self.file_table = load_file_table() if file_table is None else file_table
        self.id_table = load_id_table() if id_table is None else id_table
        self._folded = {name.casefold(): f for name, f in self.file_table.items()}
        self.records: dict[str, dict] = {}   # module file -> {param: {'max', 'default'}}

    @property
    def loaded(self) -> bool:
        return bool(self.records)

    def store(self, records: list[dict]) -> None:
        self.records = {r['file']: r['params'] for r in records}

    def resolve(self, display_name: str) -> str | None:
        """Module file for a display name: bundled table first, then an exact match of the normalised
        name against the files read from the device (e.g. effects added by newer firmware). Never guesses."""
        name = display_name.strip()
        file = self.file_table.get(name) or self._folded.get(name.casefold())
        if file:
            return file
        wanted = _norm(name)
        matches = [f for f in self.records if _norm(f.rsplit('.', 1)[0]) == wanted]
        return matches[0] if len(matches) == 1 else None

    def resolve_id(self, effect_id: int) -> str | None:
        """Module file for an effect id read from the active patch (exact, no name matching)."""
        return self.id_table.get(effect_id)

    def range_of(self, file: str | None, param: int) -> dict | None:
        """{'max', 'default'} of a parameter of a module, as read from the device, or None."""
        return self.records.get(file, {}).get(param) if file else None

    def param_range(self, display_name: str, param: int) -> tuple[str | None, dict | None]:
        """(module file, {'max', 'default'}) for a parameter, found by display name; None where unknown."""
        file = self.resolve(display_name)
        return file, self.range_of(file, param)
