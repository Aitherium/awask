"""Quiet mode: no card window, masked prompt or toast lands on top of a game.

Owner, 2026-09-23: "annoying that i keep getting awdesk/awask/awdecision cards
popping up on my main screen while im playing games".

The contract, pinned here:

    quiet = doNotDisturb OR (quietWhenFullscreen AND busy)

and a held card is HELD, never dropped: it stays open in the store.

No test here depends on the real screen. ``AITHER_QUIET`` (1 = quiet, 0 = not)
or a monkeypatched probe decides; the desk's cast file is a tmp file.
"""
from __future__ import annotations

import json
import sys

import pytest
from awask import quiet as quiet_mod
from awask.store import STATUS_OPEN, DecisionCard, DecisionOption, DecisionStore

SHIP = DecisionOption(key="ship", label="Ship it", consequence="live in ~2 min")
HOLD = DecisionOption(key="hold", label="Hold", consequence="blocks the release")
_REAL_BUSY = quiet_mod._busy


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Nothing here reads the developer's real cards, cast file or screen."""
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("DESK_CAST_FILE", str(tmp_path / "cast.json"))
    for name in ("AITHER_QUIET", "AITHER_DECISIONS_POPUP", "AITHER_DECISIONS_WEBHOOK",
                 "AITHER_DECISIONS_IGNORE_PRESENCE", "AITHER_DECISIONS_TOAST"):
        monkeypatch.delenv(name, raising=False)
    # The real probe is never consulted unless a test asks for it.
    monkeypatch.setattr(quiet_mod, "_busy", lambda: (False, ""))
    return tmp_path


def _store(tmp_path) -> DecisionStore:
    return DecisionStore(tmp_path / "decisions")


def _card(store: DecisionStore, **kw) -> DecisionCard:
    kw.setdefault("id", "")
    kw.setdefault("title", "Ship the migration now, or hold for review?")
    kw.setdefault("options", [SHIP, HOLD])
    kw.setdefault("default_key", "hold")
    return store.create(DecisionCard(**kw))


def _cast(tmp_path, **input_prefs) -> None:
    (tmp_path / "cast.json").write_text(json.dumps({"input": input_prefs}), encoding="utf-8")


# ── the rule itself ─────────────────────────────────────────────────────────────


def test_env_override_forces_both_ways(tmp_path, monkeypatch):
    _cast(tmp_path, doNotDisturb=True)
    monkeypatch.setenv("AITHER_QUIET", "0")
    assert quiet_mod.is_quiet() == (False, "")
    monkeypatch.setenv("AITHER_QUIET", "1")
    quiet, why = quiet_mod.is_quiet()
    assert quiet is True and why


def test_do_not_disturb_from_the_desk_cast_file(tmp_path):
    assert quiet_mod.is_quiet() == (False, "")
    _cast(tmp_path, doNotDisturb=True)
    assert quiet_mod.is_quiet() == (True, "Do not disturb is on")
    assert quiet_mod.quiet_reason() == "Do not disturb is on"


def test_busy_counts_only_when_quiet_when_fullscreen(tmp_path, monkeypatch):
    monkeypatch.setattr(quiet_mod, "_busy", lambda: (True, "game is full-screen"))
    # Default quietWhenFullscreen is ON.
    assert quiet_mod.is_quiet() == (True, "game is full-screen")
    _cast(tmp_path, quietWhenFullscreen=False)
    assert quiet_mod.is_quiet() == (False, "")


def test_unreadable_cast_file_is_the_defaults(tmp_path):
    (tmp_path / "cast.json").write_text("{not json", encoding="utf-8")
    assert quiet_mod.read_prefs() == (False, True)
    (tmp_path / "cast.json").write_text(
        json.dumps({"input": {"doNotDisturb": "yes"}}), encoding="utf-8")
    assert quiet_mod.read_prefs() == (False, True), "a non-boolean is not a switch"


def test_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(quiet_mod, "read_prefs", boom)
    assert quiet_mod.is_quiet() == (False, "")
    assert quiet_mod.quiet_reason() == ""


def test_off_windows_nothing_is_busy(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert _REAL_BUSY() == (False, "")


# ── notify: the raise-time window and the toast ─────────────────────────────────


def _no_popup_off_file(monkeypatch, tmp_path) -> None:
    """popup_enabled() reads ~/.aither/decisions/.popup-off; point ~ at tmp."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


def test_popup_enabled_is_false_while_quiet(tmp_path, monkeypatch):
    from awask import notify

    _no_popup_off_file(monkeypatch, tmp_path)
    monkeypatch.setenv("DISPLAY", ":0")  # so a Linux runner has a "display" too
    monkeypatch.setenv("AITHER_QUIET", "0")
    assert notify.popup_enabled() is True
    monkeypatch.setenv("AITHER_QUIET", "1")
    assert notify.popup_enabled() is False


def test_notify_holds_the_window_and_keeps_the_card(tmp_path, monkeypatch):
    from awask import notify

    store = _store(tmp_path)
    card = _card(store)
    spawned: list[str] = []
    monkeypatch.setattr(notify, "open_card_window", lambda cid: spawned.append(cid))
    _no_popup_off_file(monkeypatch, tmp_path)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("AITHER_QUIET", "1")

    result = notify.notify(card, store)

    assert spawned == [], "a card window was spawned while quiet"
    assert any("held while quiet" in s for s in result.skipped), result.skipped
    assert "card window" not in result.delivered
    held = store.get(card.id)
    assert held is not None and held.status == STATUS_OPEN, "held must not be dropped"


def test_native_toast_is_skipped_while_quiet(monkeypatch):
    from awask import notify

    sent: list[tuple] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(notify, "_linux_toast",
                        lambda *a, **k: sent.append((a, k)))
    monkeypatch.setenv("AITHER_DECISIONS_TOAST", "1")
    monkeypatch.setenv("AITHER_QUIET", "1")

    error = notify.native_toast("title", "body")

    assert sent == [], "a toast went out while quiet"
    assert error and "held while quiet" in error


# ── the masked credential prompt ────────────────────────────────────────────────


def test_credential_prompt_is_held_while_quiet(tmp_path, monkeypatch):
    from awask import secure_prompt

    store = _store(tmp_path)
    card = store.create(DecisionCard(id="", kind="credential", title="Need the deploy token",
                                     secret_name="DEPLOY_TOKEN",
                                     credential_format="api_key",
                                     credential_description="the deploy lane needs it"))
    monkeypatch.setenv("AITHER_QUIET", "1")

    launched, why = secure_prompt.launch_gui_prompt(card)

    assert launched is False
    assert why.startswith("held while quiet: ")
    assert f"answer with awask answer {card.id}" in why
    assert store.get(card.id).status == STATUS_OPEN


# ── winproc: the terminal-focus path ────────────────────────────────────────────


class _FakeWinDLL:
    """Records every user32/kernel32 call instead of reaching the real desktop."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.user32 = self
        self.kernel32 = self

    def __getattr__(self, name: str):
        def call(*_a, **_k):
            self.calls.append(name)
            return 1 if name == "SetForegroundWindow" else 0
        return call


def test_focus_window_never_takes_the_foreground_while_quiet(monkeypatch):
    from awask import winproc

    fake = _FakeWinDLL()
    monkeypatch.setattr(winproc, "IS_WINDOWS", True)
    monkeypatch.setattr(winproc.ctypes, "windll", fake, raising=False)

    monkeypatch.setenv("AITHER_QUIET", "1")
    assert winproc.focus_window(1234) is False
    assert fake.calls == [], f"touched the desktop while quiet: {fake.calls}"

    monkeypatch.setenv("AITHER_QUIET", "0")
    assert winproc.focus_window(1234) is True
    assert "SetForegroundWindow" in fake.calls, "the not-quiet path no longer focuses"


# ── presence / DM routing: a held card must still reach the phone ───────────────


#
# QUIET counts as AWAY through ONE seam: channels suppresses the DM only when
# `is_at_desk() and popup_enabled()`, and popup_enabled() is False while quiet.
# presence.is_at_desk stays pure idle-time arithmetic (the adk twin is the same).


def test_dm_is_delivered_for_an_at_desk_owner_while_quiet(tmp_path, monkeypatch):
    from awask import presence
    from awask.channels import ChannelConfig, DecisionChannelBridge

    store = _store(tmp_path)
    card = _card(store)
    cfg = ChannelConfig(platform="discord", enabled=True, owner_user_id="1",
                        deliver_to="1")
    bridge = DecisionChannelBridge(store=store, configs={"discord": cfg})
    monkeypatch.setattr(presence, "desktop_idle_seconds", lambda: 1.0)  # at the desk
    _no_popup_off_file(monkeypatch, tmp_path)
    monkeypatch.setenv("DISPLAY", ":0")

    monkeypatch.setenv("AITHER_QUIET", "0")
    assert bridge.should_deliver(card, cfg) is False, "at desk, popup shows: no DM"
    monkeypatch.setenv("AITHER_QUIET", "1")
    assert bridge.should_deliver(card, cfg) is True, "popup held: the DM must carry it"


# ── the card window itself ──────────────────────────────────────────────────────
#
# Each window scenario runs in a CHILD interpreter. In-process Tk under pytest's
# output capture is flaky on Windows: Tcl caches the standard channels of the
# first interpreter it creates, and later Tk inits then intermittently fail to
# read their own library ("couldn't read file .../tk8.6/ttk/ttk.tcl"). Measured
# 2026-09-23: 1-2 of 5 window tests red per captured run, 0 of 10 with -s, and
# capfd.disabled() did not help. A child owns its own real channels.


def _tk_usable() -> bool:
    import subprocess

    probe = "import tkinter; r = tkinter.Tk(); r.withdraw(); r.destroy()"
    try:
        return subprocess.run([sys.executable, "-c", probe], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_tk = pytest.mark.skipif(not _tk_usable(), reason="no usable Tk display")


def _run_scenario(name: str, tmp_path) -> None:
    """Run ``_scenario_<name>`` in a child; its assertion failures fail the test."""
    import os
    import subprocess
    from pathlib import Path

    here = Path(__file__).resolve().parent
    code = (
        "import sys; sys.path[:0] = [%r, %r]; import test_quiet as t; "
        "t._scenario_%s(%r)" % (str(here.parent), str(here), name, str(tmp_path))
    )
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr[-3000:] or proc.stdout[-3000:]


class _Calls:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, *_a, **_k) -> None:
        self.n += 1


def _spy_tk() -> dict:
    """Record lift/focus/deiconify instead of doing them: nothing reaches the screen."""
    import tkinter

    from awask import popup

    calls = {name: _Calls() for name in ("lift", "focus_force", "deiconify")}
    for name, spy in calls.items():
        setattr(tkinter.Tk, name, spy)
    popup.CardWindow._place = lambda self: None
    return calls


def _child_setup(tmp: str) -> DecisionStore:
    import os
    from pathlib import Path

    os.environ["AITHER_DECISIONS_DIR"] = str(Path(tmp) / "decisions")
    os.environ["DESK_CAST_FILE"] = str(Path(tmp) / "cast.json")
    quiet_mod._busy = lambda: (False, "")
    return _store(Path(tmp))


def _scenario_held(tmp: str) -> None:
    import os

    from awask import popup

    store = _child_setup(tmp)
    spied = _spy_tk()
    card = _card(store, urgency="high")
    os.environ["AITHER_QUIET"] = "1"

    window = popup.CardWindow(store, start_id=card.id)
    assert window.root.state() == "withdrawn"
    assert window._held_reason
    assert window._hold_job is not None, "nothing polls for the end of quiet"
    assert (spied["lift"].n, spied["focus_force"].n) == (0, 0)

    window._release_when_not_quiet()          # still quiet: still held
    assert spied["deiconify"].n == 0 and window._held_reason

    os.environ["AITHER_QUIET"] = "0"
    window._release_when_not_quiet()          # quiet over: show it
    assert spied["deiconify"].n == 1
    assert spied["lift"].n >= 1 and spied["focus_force"].n >= 1
    assert not window._held_reason
    assert store.get(card.id).status == STATUS_OPEN, "holding answered nothing"
    window.root.destroy()


def _scenario_no_relift(tmp: str) -> None:
    import os

    from awask import popup

    store = _child_setup(tmp)
    spied = _spy_tk()
    first = _card(store, urgency="high")
    os.environ["AITHER_QUIET"] = "0"
    window = popup.CardWindow(store, start_id=first.id)
    assert int(window.root.attributes("-topmost")) == 1
    lifts, focus = spied["lift"].n, spied["focus_force"].n

    os.environ["AITHER_QUIET"] = "1"           # a game went full-screen
    _card(store, title="Another session's card", urgency="critical")
    window._tick()                             # absorbs it, re-renders

    assert len(window.queue) == 2, "the new card was not absorbed"
    assert (spied["lift"].n, spied["focus_force"].n) == (lifts, focus), "re-lifted"
    assert int(window.root.attributes("-topmost")) == 0, "still above the game"

    os.environ["AITHER_QUIET"] = "0"
    window._tick()
    assert int(window.root.attributes("-topmost")) == 1
    window.root.destroy()


def _scenario_unpin(tmp: str) -> None:
    import os

    from awask import popup

    store = _child_setup(tmp)
    card = _card(store)
    os.environ["AITHER_QUIET"] = "0"
    window = popup.CardWindow(store, start_id=card.id, headless=True)
    window._toggle_pin()
    assert int(window.root.attributes("-topmost")) == 1
    window._toggle_pin()
    assert int(window.root.attributes("-topmost")) == 0, "unpin left it on top"
    window.root.destroy()


@needs_tk
def test_window_is_held_withdrawn_then_shown_when_quiet_ends(tmp_path):
    _run_scenario("held", tmp_path)


@needs_tk
def test_open_window_does_not_jump_over_a_game_on_new_arrivals(tmp_path):
    _run_scenario("no_relift", tmp_path)


@needs_tk
def test_unpin_really_clears_topmost(tmp_path):
    _run_scenario("unpin", tmp_path)
