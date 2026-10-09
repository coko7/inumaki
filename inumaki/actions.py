import os
import shlex
import shutil
import subprocess
import sys
import time
from typing import ClassVar, cast

from .apps import AppIndex
from .config import Action, Coord, Settings
from .util import log


class Executor:
    BUTTONS_YDO: ClassVar[dict[str, str]] = {
        "left": "0xC0",
        "right": "0xC1",
        "middle": "0xC2",
    }
    BUTTONS_XDO: ClassVar[dict[str, str]] = {"left": "1", "right": "3", "middle": "2"}

    def __init__(self, settings: Settings, dry_run: bool = False):
        self.screen: list[int] = settings["screen"]
        self.launcher: str = settings["launcher"]
        self.apps: AppIndex = AppIndex()
        self.dry_run: bool = dry_run

        wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland"
        if wayland or not shutil.which("xdotool"):
            self.backend: str = "ydotool"
        else:
            self.backend = "xdotool"

        if not shutil.which(self.backend) and not dry_run:
            sys.exit(f"{self.backend} not found in PATH")

    def _run(self, *args: str | int, shell: bool = False) -> None:
        if self.dry_run:
            log(f"  (dry-run) {args[0] if shell else ' '.join(map(str, args))}")
            return

        if shell:
            _ = subprocess.Popen(str(args[0]), shell=True)
        else:
            _ = subprocess.run([str(a) for a in args], check=False)

    def _coord(self, value: Coord, axis: int) -> int:
        if isinstance(value, str) and value.endswith("%"):
            return int(self.screen[axis] * float(value[:-1]) / 100)
        return int(value)

    def launch(self, spoken: str) -> None:
        found = self.apps.find(spoken)
        if not found:
            self.apps.refresh()  # maybe installed since startup
            found = self.apps.find(spoken)
        if not found:
            log(f'  no installed app matches "{spoken}"')
            return

        app_id, name = found
        log(f"  launching {name} ({app_id})")
        cmd = [part.format(id=app_id) for part in shlex.split(self.launcher)]
        if self.dry_run:
            log(f"  (dry-run) {' '.join(cmd)}")
            return

        _ = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def run(self, action: Action, params: dict[str, str] | None = None) -> None:
        if params:  # fill "{name}" placeholders from regex captures
            filled: dict[str, object] = {
                k: v.format_map(params) if isinstance(v, str) else v
                for k, v in action.items()
            }
            # Same keys, strings stay strings: still the same kind of Action.
            action = cast(Action, cast(object, filled))
        ydo = self.backend == "ydotool"

        # Compare action["type"] directly (not via a variable) so the type
        # checker narrows `action` to the matching TypedDict in each branch.
        if action["type"] == "move":
            x, y = self._coord(action["x"], 0), self._coord(action["y"], 1)
            if ydo:
                self._run("ydotool", "mousemove", "--absolute", "-x", x, "-y", y)
            else:
                self._run("xdotool", "mousemove", x, y)

        elif action["type"] == "move_relative":
            dx, dy = int(action.get("dx", 0)), int(action.get("dy", 0))
            if ydo:
                self._run("ydotool", "mousemove", "-x", dx, "-y", dy)
            else:
                self._run("xdotool", "mousemove_relative", "--", dx, dy)

        elif action["type"] == "click":
            button = action.get("button", "left")
            count = int(action.get("count", 1))
            if ydo:
                self._run(
                    "ydotool",
                    "click",
                    "--repeat",
                    count,
                    "--next-delay",
                    80,
                    self.BUTTONS_YDO[button],
                )
            else:
                self._run(
                    "xdotool", "click", "--repeat", count, self.BUTTONS_XDO[button]
                )

        elif action["type"] == "scroll":
            amount = int(action.get("amount", 3))  # positive = down
            if ydo:
                self._run("ydotool", "mousemove", "--wheel", "-x", 0, "-y", -amount)
            else:
                btn = "5" if amount > 0 else "4"
                self._run("xdotool", "click", "--repeat", abs(amount), btn)

        elif action["type"] == "type":
            text = action["text"]
            if ydo:
                self._run("ydotool", "type", "--", text)
            else:
                self._run("xdotool", "type", "--", text)

        elif action["type"] == "key":
            # ydotool: raw "keycode:state" list, e.g. "29:1 46:1 46:0 29:0"
            # xdotool: keysym combo, e.g. "ctrl+c"
            self._run(self.backend, "key", *str(action["keys"]).split())

        elif action["type"] == "shell":
            self._run(action["cmd"], shell=True)

        elif action["type"] == "launch":
            self.launch(action["app"])

        elif action["type"] == "sleep":
            time.sleep(float(action.get("ms", 100)) / 1000)

        # No else: load_config() rejects unknown action types at startup.
