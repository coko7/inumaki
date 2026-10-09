import difflib
import os
from pathlib import Path

from .util import normalize

# (desktop id, display name, [normalized aliases])
AppEntry = tuple[str, str, list[str]]


def application_dirs() -> list[Path]:
    data_home = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")

    # Earlier dirs win, so user entries override system ones.
    return [Path(d) / "applications" for d in [data_home, *data_dirs.split(":")] if d]


def parse_desktop_entry(path: Path) -> dict[str, str] | None:
    entry: dict[str, str] = {}
    in_main = False

    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None

    for line in lines:
        line = line.strip()
        if line.startswith("["):
            in_main = line == "[Desktop Entry]"
        elif in_main and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            _ = entry.setdefault(key.strip(), value.strip())

    return entry


class AppIndex:
    """Spoken name -> desktop file id, built from installed .desktop files."""

    def __init__(self) -> None:
        self.apps: list[AppEntry] | None = None  # loaded on first find()

    def refresh(self) -> None:
        self.apps = self._scan()

    def _entries(self) -> list[AppEntry]:
        if self.apps is None:
            self.apps = self._scan()
        return self.apps

    def _scan(self) -> list[AppEntry]:
        seen: set[str] = set()
        apps: list[AppEntry] = []
        for app_dir in application_dirs():
            if not app_dir.is_dir():
                continue

            for path in sorted(app_dir.rglob("*.desktop")):
                app_id = str(path.relative_to(app_dir)).replace("/", "-")[
                    : -len(".desktop")
                ]
                if app_id in seen:
                    continue

                seen.add(app_id)
                desktop_entry = parse_desktop_entry(path)
                if (
                    not desktop_entry
                    or desktop_entry.get("Type") != "Application"
                    or desktop_entry.get("NoDisplay") == "true"
                    or desktop_entry.get("Hidden") == "true"
                ):
                    continue

                name = desktop_entry.get("Name", app_id)
                aliases = [
                    name,
                    desktop_entry.get("GenericName", ""),
                    # Take last word as many apps use reverse-domain IDs:
                    # - org.kde.dolphin
                    # - com.mitchellh.ghostty.desktop
                    # - org.gnome.Nautilus
                    # etc.
                    app_id.split(".")[-1],
                ]
                aliases += desktop_entry.get("Keywords", "").split(";")
                aliases = [a for a in dict.fromkeys(map(normalize, aliases)) if a]
                apps.append((app_id, name, aliases))
        return apps

    def find(self, spoken: str) -> tuple[str, str] | None:
        apps = self._entries()

        spoken = normalize(spoken)
        squashed = spoken.replace(" ", "")

        # Exact display name, then exact alias (spaces ignored: "libre office").
        for app_id, name, aliases in apps:
            if normalize(name).replace(" ", "") == squashed:
                return app_id, name

        for app_id, name, aliases in apps:
            if any(a.replace(" ", "") == squashed for a in aliases):
                return app_id, name

        best: tuple[str, str] | None = None
        best_score = 0.0
        for app_id, name, aliases in apps:
            for i, alias in enumerate(aliases):
                score = difflib.SequenceMatcher(None, alias, spoken).ratio()
                score -= 0.05 * min(i, 3)  # prefer name over keywords
                if score > best_score:
                    best, best_score = (app_id, name), score

        return best if best_score >= 0.6 else None
