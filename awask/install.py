"""``awask install-hooks`` — wire the card loop into Claude Code.

Installing awask gives you the CLI. It does NOT, by itself, make anything happen:
cards appear only when an agent types the command, and your answer reaches the run
only at your next prompt. The loop is three hooks, and this writes them.

    awask install-hooks              # this project (./.claude)
    awask install-hooks --user       # every project (~/.claude)
    awask install-hooks --dry-run    # show what would change, write nothing

What it writes
--------------
Three files into ``<target>/hooks/``, and three entries into ``<target>/settings.json``:

  Stop              stop_awask_cards.py         holds the turn open for your answer
  UserPromptSubmit  awask_mailbox_drain.py      carries your answer INTO the session
  Notification      awask_notification_card.py  "the agent is waiting" becomes a card

The middle one is the one people skip and the one that matters: without it you can
answer a card and the agent never learns you did.

Three rules this follows, each of which is a way installers go wrong
-------------------------------------------------------------------
1. **settings.json is MERGED, never replaced.** Your existing hooks are read,
   preserved, and this appends only what is missing. Re-running is a no-op — the
   entries are matched by command path, so an install is idempotent rather than
   additive. An installer that stomps a config is a worse outcome than one that
   does nothing.

2. **It re-asserts after acting.** Every hook is executed with ``--self-test`` once
   written. A copy that reports success without proving the file runs is exactly
   how a broken install looks identical to a working one.

3. **It reports what it did NOT do.** A skipped file, an unreadable settings.json,
   an entry already present — each prints. A run that quietly bounds its own scope
   reads as "installed everything".
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

#: hook filename -> Claude Code event it binds to.
HOOKS = {
    "stop_awask_cards.py": "Stop",
    "awask_mailbox_drain.py": "UserPromptSubmit",
    "awask_notification_card.py": "Notification",
}


def hook_source_dir() -> Path:
    return Path(__file__).resolve().parent / "hooks"


def target_dir(user: bool, project: Path | None = None) -> Path:
    if user:
        return Path.home() / ".claude"
    return (project or Path.cwd()) / ".claude"


def _command_for(dest: Path) -> str:
    """The command string written into settings.json.

    Absolute, and quoted: a relative path breaks the moment Claude Code runs the
    hook from a different working directory, and an unquoted one breaks on any
    path with a space -- which on Windows is most of them.
    """
    return '"%s" "%s"' % (sys.executable, dest)


def copy_hooks(target: Path, dry_run: bool) -> tuple[list[str], list[str]]:
    """Returns (written, notes)."""
    written: list[str] = []
    notes: list[str] = []
    src = hook_source_dir()
    dest_dir = target / "hooks"
    for name in sorted(HOOKS):
        source = src / name
        if not source.is_file():
            notes.append("MISSING from the installed package: %s" % name)
            continue
        dest = dest_dir / name
        if dry_run:
            written.append("%s -> %s" % (name, dest))
            continue
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            written.append(str(dest))
        except OSError as exc:
            notes.append("could not write %s: %s" % (dest, exc))
    return written, notes


def merge_settings(target: Path, dry_run: bool) -> tuple[list[str], list[str]]:
    """Add the three hook entries to settings.json without disturbing anything else."""
    added: list[str] = []
    notes: list[str] = []
    settings_path = target / "settings.json"

    data: dict = {}
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                notes.append("settings.json is not a JSON object -- refusing to touch it")
                return added, notes
        except (OSError, ValueError) as exc:
            # Refuse rather than overwrite. A malformed settings.json is somebody's
            # work in progress, and replacing it is unrecoverable for them.
            notes.append("settings.json is unreadable (%s) -- refusing to overwrite it" % exc)
            return added, notes

    hooks_cfg = data.setdefault("hooks", {})
    if not isinstance(hooks_cfg, dict):
        notes.append("settings.json `hooks` is not an object -- refusing to touch it")
        return added, notes

    for name, event in sorted(HOOKS.items()):
        command = _command_for(target / "hooks" / name)
        matchers = hooks_cfg.setdefault(event, [])
        if not isinstance(matchers, list):
            notes.append("settings.json hooks.%s is not a list -- skipped" % event)
            continue

        # Idempotence is matched on the hook FILENAME, not the whole command string:
        # the interpreter path changes between a venv and a system python, and
        # matching the full string would silently install a second copy every time
        # the user switched environments.
        already = False
        for group in matchers:
            if not isinstance(group, dict):
                continue
            for entry in group.get("hooks", []) or []:
                if isinstance(entry, dict) and name in str(entry.get("command", "")):
                    already = True
        if already:
            notes.append("already wired: %s -> %s" % (event, name))
            continue

        matchers.append({"matcher": "", "hooks": [{"type": "command", "command": command}]})
        added.append("%s -> %s" % (event, name))

    if added and not dry_run:
        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            notes.append("could not write settings.json: %s" % exc)
            return [], notes
    return added, notes


def verify(target: Path) -> tuple[list[str], list[str]]:
    """Run each installed hook's --self-test. A copy is not an install."""
    passed: list[str] = []
    failed: list[str] = []
    for name in sorted(HOOKS):
        dest = target / "hooks" / name
        if not dest.is_file():
            failed.append("%s (not installed)" % name)
            continue
        try:
            r = subprocess.run(
                [sys.executable, str(dest), "--self-test"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            failed.append("%s (could not run: %s)" % (name, exc))
            continue
        (passed if r.returncode == 0 else failed).append(name)
    return passed, failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="awask install-hooks",
        description="Wire the awask card loop into Claude Code.",
    )
    ap.add_argument("--user", action="store_true",
                    help="install into ~/.claude (every project) instead of ./.claude")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("--self-test", action="store_true", help="prove the installer's own rules")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    target = target_dir(args.user)
    print("awask install-hooks -> %s" % target)
    if args.dry_run:
        print("  (dry run: nothing will be written)")

    written, copy_notes = copy_hooks(target, args.dry_run)
    added, merge_notes = merge_settings(target, args.dry_run)

    for line in written:
        print("  wrote    %s" % line)
    for line in added:
        print("  wired    %s" % line)
    for line in copy_notes + merge_notes:
        print("  note     %s" % line)

    if args.dry_run:
        print("\nDry run complete. Re-run without --dry-run to apply.")
        return 0

    passed, failed = verify(target)
    for name in passed:
        print("  verified %s" % name)
    for name in failed:
        print("  FAILED   %s" % name)

    if failed:
        print("\nINSTALL INCOMPLETE - %d hook(s) did not pass their own self-test." % len(failed))
        return 1

    print("\nInstalled and verified. Raise a card with:")
    print('  awask ask "your question" --option "a|A|what happens" '
          '--option "b|B|what happens" --default a')
    return 0


def self_test() -> int:
    import tempfile

    ok = True

    def check(label, cond):
        nonlocal ok
        print("  %s %s" % ("ok  " if cond else "FAIL", label))
        ok = ok and cond

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / ".claude"

        # A pre-existing settings.json with the user's own hook must survive.
        target.mkdir(parents=True)
        (target / "settings.json").write_text(
            json.dumps({
                "model": "opus",
                "hooks": {"Stop": [{"matcher": "", "hooks": [
                    {"type": "command", "command": "python mine.py"}]}]},
            }),
            encoding="utf-8",
        )

        added, _ = merge_settings(target, dry_run=False)
        data = json.loads((target / "settings.json").read_text(encoding="utf-8"))
        check("adds one entry per event", len(added) == 3)
        check("preserves unrelated settings", data.get("model") == "opus")
        commands = [
            e.get("command", "")
            for g in data["hooks"]["Stop"] for e in g.get("hooks", [])
        ]
        check("preserves the user's own Stop hook", any("mine.py" in c for c in commands))
        check("adds its own Stop hook alongside", any("stop_awask_cards.py" in c for c in commands))

        added2, notes2 = merge_settings(target, dry_run=False)
        check("re-running adds nothing (idempotent)", added2 == [])
        check("re-running says why", any("already wired" in n for n in notes2))

        # An interpreter change must NOT install a second copy.
        data = json.loads((target / "settings.json").read_text(encoding="utf-8"))
        for g in data["hooks"]["Stop"]:
            for e in g.get("hooks", []):
                if "stop_awask_cards.py" in e.get("command", ""):
                    e["command"] = '"/other/python" ' + e["command"].split(" ", 1)[1]
        (target / "settings.json").write_text(json.dumps(data), encoding="utf-8")
        added3, _ = merge_settings(target, dry_run=False)
        check("a different interpreter does not duplicate the entry",
              not any("Stop" in a for a in added3))

        # A malformed settings.json is refused, never overwritten.
        bad = Path(tmp) / "bad" / ".claude"
        bad.mkdir(parents=True)
        (bad / "settings.json").write_text("{not json", encoding="utf-8")
        added4, notes4 = merge_settings(bad, dry_run=False)
        check("refuses to overwrite an unreadable settings.json",
              added4 == [] and any("refusing" in n for n in notes4))
        check("and leaves the original bytes alone",
              (bad / "settings.json").read_text(encoding="utf-8") == "{not json")

        # dry-run writes nothing.
        dry = Path(tmp) / "dry" / ".claude"
        merge_settings(dry, dry_run=True)
        check("dry run writes no settings.json", not (dry / "settings.json").exists())
        w, _ = copy_hooks(dry, dry_run=True)
        check("dry run copies no hooks", not (dry / "hooks").exists() and len(w) == 3)

    print("self-test", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
