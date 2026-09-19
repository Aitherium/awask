"""A card recipe must raise ONCE per streak, and its answer must actually act.

Both halves have a silent failure mode, and both were the reason this exists:

* A producer calls the CLI on every failing pass. Without a dedupe key that
  matches CLOSED cards too, answering the card is what *causes* the next one — so
  a green "the card was raised" test is exactly what a card storm looks like.
* These cards have no session, so the text-steering path returns before doing
  anything. A hook hung off delivery would therefore run for every card EXCEPT
  the ones that need it, and the card would close reporting an action nobody
  took.

So every refusal below is paired with the positive path it guards, and the proof
that nothing ran is a marker file the fake producer writes when it does run.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from awask.card_recipes import (
    CARD_RECIPES,
    CardRecipeError,
    apply_answer,
    build_card,
    check_all,
    dedupe_prefix,
)
from awask.store import DecisionStore

VARS = {
    "job": "nightly-sync",
    "first_failure_ts": "2026-09-18T05:00:01+00:00",
    "n": "3",
    "last_state": "failure",
    "last_reason": "exit 1",
    "last_wake_id": "w-7hk2m9pq",
    "every": "1h",
    "run": "python sync.py",
}


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    """Where the fake producer records that it RAN. Its absence is the assertion."""
    return tmp_path / "ran.txt"


@pytest.fixture
def fake_awrise(tmp_path: Path, marker: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A producer CLI that writes its argv to ``marker`` and exits 0."""
    script = _write_fake(tmp_path, marker, exit_code=0, sleep_seconds=0.0)
    monkeypatch.setenv("AWRISE_BIN", str(script))
    return script


def _write_fake(tmp_path: Path, marker: Path, *, exit_code: int,
                sleep_seconds: float) -> Path:
    """A real executable on this platform that records exactly the argv it got.

    A shell one-liner cannot do that: ``%*`` and ``$*`` both flatten the list, so
    an argv assertion built on them would pass for a command that had been
    re-split by a shell — which is the one failure the argv-list rule exists to
    prevent. So the script is a two-line launcher around a python recorder.
    """
    recorder = tmp_path / "recorder.py"
    recorder.write_text(
        "import json, sys, time\n"
        f"time.sleep({sleep_seconds!r})\n"
        f"open({str(marker)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        script = tmp_path / f"awrise-{exit_code}-{sleep_seconds}.cmd"
        script.write_text(
            "@echo off\r\n"
            f'"{Path(os.sys.executable)}" "{recorder}" %*\r\n'
            "exit /b %ERRORLEVEL%\r\n",
            encoding="utf-8")
    else:
        script = tmp_path / f"awrise-{exit_code}-{sleep_seconds}.sh"
        script.write_text(
            "#!/bin/sh\n"
            f'exec "{Path(os.sys.executable)}" "{recorder}" "$@"\n',
            encoding="utf-8")
        script.chmod(0o755)
    return script


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DecisionStore:
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "cards"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    return DecisionStore(tmp_path / "cards")


# ── the recipe itself ─────────────────────────────────────────────────────────


class TestRecipeShape:
    def test_every_shipped_recipe_is_answerable(self) -> None:
        assert check_all() == []

    def test_options_and_actions_are_the_same_set(self) -> None:
        recipe = CARD_RECIPES["wake-failed"]
        assert sorted(o["key"] for o in recipe["options"]) == sorted(recipe["steerback"])

    def test_the_default_never_spawns(self) -> None:
        recipe = CARD_RECIPES["wake-failed"]
        # A free-text reply and a deadline both apply the default without the
        # owner picking it, so this is the one entry that MUST be inert.
        assert recipe["default_key"] == "keep"
        assert recipe["steerback"][recipe["default_key"]] is None

    def test_renders_the_variables_into_the_card(self) -> None:
        card = build_card("wake-failed", VARS)
        assert card.title == "Wake 'nightly-sync' has failed 3x in a row"
        assert "1h" in card.summary and "exit 1" in card.summary
        assert "python sync.py" in card.detail
        assert "streak: 3" in card.facts
        assert card.urgency == "high"
        assert card.default_key == "keep"
        assert card.card_recipe == "wake-failed"
        assert card.recipe_vars["job"] == "nightly-sync"

    def test_deadline_is_set_so_a_stale_card_self_closes(self) -> None:
        card = build_card("wake-failed", VARS, now=1_000_000.0)
        assert card.deadline == pytest.approx(1_000_000.0 + 24 * 3600)

    def test_missing_required_vars_name_themselves(self) -> None:
        with pytest.raises(CardRecipeError) as exc:
            build_card("wake-failed", {"job": "nightly-sync"})
        assert "first_failure_ts" in str(exc.value) and "n" in str(exc.value)

    def test_an_unknown_var_is_refused_rather_than_ignored(self) -> None:
        with pytest.raises(CardRecipeError):
            build_card("wake-failed", {**VARS, "home_dir": "/tmp"})

    def test_an_unknown_recipe_is_refused(self) -> None:
        with pytest.raises(CardRecipeError):
            build_card("no-such-recipe", VARS)

    @pytest.mark.parametrize("bad", ["x; rm -rf /", "", "-name", "../x", "a b", "z" * 65])
    def test_raise_time_refuses_an_unsafe_job_name(self, bad: str) -> None:
        with pytest.raises(CardRecipeError):
            build_card("wake-failed", {**VARS, "job": bad})

    def test_long_fields_are_truncated_so_the_options_stay_on_screen(self) -> None:
        card = build_card("wake-failed", {**VARS, "output_tail": "y" * 5000})
        assert len(card.recipe_vars["output_tail"]) == 600

    def test_the_dedupe_key_is_streak_scoped(self) -> None:
        card = build_card("wake-failed", VARS)
        assert card.dedupe_key == "awrise:wake-failed:nightly-sync:2026-09-18T05:00:01+00:00"
        assert dedupe_prefix("wake-failed", VARS) == "awrise:wake-failed:nightly-sync:"


# ── idempotency ───────────────────────────────────────────────────────────────


class TestDedupe:
    def test_a_second_raise_returns_the_open_card_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path, monkeypatch)
        first = store.create(build_card("wake-failed", VARS))
        again = store.create(build_card("wake-failed", VARS))
        assert again.id == first.id
        assert len(list((tmp_path / "cards").glob("d-*.json"))) == 1

    @pytest.mark.parametrize("closer", ["answer", "cancel"])
    def test_a_closed_card_is_not_re_raised_for_the_same_streak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closer: str
    ) -> None:
        # This is the whole point. Before the closed-card guard, answering the
        # card is what produced the next one, so the storm got WORSE the moment
        # the owner engaged with it.
        store = _store(tmp_path, monkeypatch)
        first = store.create(build_card("wake-failed", VARS))
        if closer == "answer":
            store.answer(first.id, "keep", via="test", deliver=False)
        else:
            store.cancel(first.id, note="test")
        again = store.create(build_card("wake-failed", VARS))
        assert again.id == first.id
        assert len(list((tmp_path / "cards").glob("d-*.json"))) == 1

    def test_a_second_open_card_for_the_same_job_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path, monkeypatch)
        first = store.create(build_card("wake-failed", VARS))
        shifted = store.create(build_card(
            "wake-failed", {**VARS, "first_failure_ts": "2026-09-18T06:00:01+00:00"}))
        assert shifted.id == first.id
        assert len(list((tmp_path / "cards").glob("d-*.json"))) == 1

    def test_a_new_streak_after_the_first_card_closed_does_raise_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path, monkeypatch)
        first = store.create(build_card("wake-failed", VARS))
        store.answer(first.id, "keep", via="test", deliver=False)
        later = store.create(build_card(
            "wake-failed", {**VARS, "first_failure_ts": "2026-09-19T05:00:01+00:00"}))
        assert later.id != first.id

    def test_a_different_job_is_a_different_card(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path, monkeypatch)
        first = store.create(build_card("wake-failed", VARS))
        other = store.create(build_card("wake-failed", {**VARS, "job": "hourly-gc"}))
        assert other.id != first.id

    def test_a_card_with_no_dedupe_key_is_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guard must not quietly collapse ordinary cards, which carry no key.
        from awask.store import DecisionCard, DecisionOption

        store = _store(tmp_path, monkeypatch)
        def plain() -> DecisionCard:
            return DecisionCard(
                id="", title="ship or hold?", kind="decision", default_key="hold",
                options=[DecisionOption(key="ship", label="Ship it"),
                         DecisionOption(key="hold", label="Hold")])
        assert store.create(plain()).id != store.create(plain()).id

    def test_a_malformed_dedupe_key_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from awask.store import DecisionCard, DecisionError

        store = _store(tmp_path, monkeypatch)
        card = DecisionCard(id="", title="t", kind="info", dedupe_key="a b\nc")
        with pytest.raises(DecisionError):
            store.create(card)


# ── the answer becomes an action ──────────────────────────────────────────────


class TestApplyAnswer:
    def test_disable_runs_the_exact_argv_list(
        self, tmp_path: Path, marker: Path, fake_awrise: Path
    ) -> None:
        import json

        card = build_card("wake-failed", VARS)
        card.answer = "disable"
        applied, why = apply_answer(card)
        assert applied, why
        assert "exit 0" in why
        assert json.loads(marker.read_text(encoding="utf-8")) == [
            "disable", "--name", "nightly-sync"]

    def test_a_non_zero_exit_is_reported_not_swallowed(
        self, tmp_path: Path, marker: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = _write_fake(tmp_path, marker, exit_code=3, sleep_seconds=0.0)
        monkeypatch.setenv("AWRISE_BIN", str(script))
        card = build_card("wake-failed", VARS)
        card.answer = "disable"
        applied, why = apply_answer(card)
        assert not applied
        assert "exit 3" in why

    def test_keep_is_a_no_op_and_spawns_nothing(
        self, marker: Path, fake_awrise: Path
    ) -> None:
        card = build_card("wake-failed", VARS)
        card.answer = "keep"
        applied, why = apply_answer(card)
        assert applied and why == "no-op"
        assert not marker.exists()

    @pytest.mark.parametrize("bad", ["x; rm -rf /", "", "-name", "../x", 17, None])
    def test_apply_time_refuses_a_job_name_rewritten_on_disk(
        self, marker: Path, fake_awrise: Path, bad: object
    ) -> None:
        # The input here is the card JSON, which anything with write access to
        # the card directory can edit — so the raise-time check is not the check
        # that matters, and this is the one that keeps argv safe.
        card = build_card("wake-failed", VARS)
        card.answer = "disable"
        card.recipe_vars = {**card.recipe_vars, "job": bad}
        applied, why = apply_answer(card)
        assert not applied
        assert "invalid at apply time" in why
        assert not marker.exists()

    def test_a_binary_that_is_not_a_file_refuses_without_spawning(
        self, tmp_path: Path, marker: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWRISE_BIN", str(tmp_path / "there-is-no-such-file"))
        card = build_card("wake-failed", VARS)
        card.answer = "disable"
        applied, why = apply_answer(card)
        assert not applied and "not found" in why
        assert not marker.exists()

    def test_an_answer_with_no_action_entry_refuses(
        self, marker: Path, fake_awrise: Path
    ) -> None:
        card = build_card("wake-failed", VARS)
        card.answer = "something-else"
        applied, why = apply_answer(card)
        assert not applied and "no action" in why
        assert not marker.exists()

    def test_a_card_with_no_recipe_is_silent(self) -> None:
        from awask.store import DecisionCard

        applied, why = apply_answer(DecisionCard(id="d-x", title="t", answer="disable"))
        assert not applied and why == ""


# ── the hook the store runs ───────────────────────────────────────────────────


class TestStoreTransitionRunsIt:
    @pytest.mark.parametrize("deliver", [False, True])
    def test_sessionless_card_disable_runs_argv(
        self, tmp_path: Path, marker: Path, fake_awrise: Path,
        monkeypatch: pytest.MonkeyPatch, deliver: bool
    ) -> None:
        # deliver=True is the proof that matters: the mailbox path returns early
        # for a card with no session id, so a hook that lived there would run for
        # neither value.
        import json

        store = _store(tmp_path, monkeypatch)
        card = store.create(build_card("wake-failed", VARS))
        assert card.source.session_id == ""
        answered = store.answer(card.id, "disable", via="test", deliver=deliver)
        assert json.loads(marker.read_text(encoding="utf-8")) == [
            "disable", "--name", "nightly-sync"]
        assert any("Steerback" in n.text for n in answered.notes)
        assert any("exit 0" in n.text for n in answered.notes)

    def test_the_outcome_is_persisted_not_just_returned(
        self, tmp_path: Path, marker: Path, fake_awrise: Path,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path, monkeypatch)
        card = store.create(build_card("wake-failed", VARS))
        store.answer(card.id, "disable", via="test", deliver=False)
        reread = store.get(card.id)
        assert reread is not None
        assert any("Steerback" in n.text for n in reread.notes)

    def test_run_now_returns_before_the_child_exits(
        self, tmp_path: Path, marker: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = _write_fake(tmp_path, marker, exit_code=0, sleep_seconds=3.0)
        monkeypatch.setenv("AWRISE_BIN", str(script))
        store = _store(tmp_path, monkeypatch)
        card = store.create(build_card("wake-failed", VARS))
        started = time.monotonic()
        answered = store.answer(card.id, "run_now", via="test", deliver=False)
        elapsed = time.monotonic() - started
        # The answering process is the owner's reply channel. Waiting out a
        # scheduled job there stalls it, and a timeout kill would abort the run
        # the owner just asked for.
        assert elapsed < 2.0, f"answering blocked for {elapsed:.1f}s"
        assert not marker.exists(), "the child had already finished — it was waited on"
        assert any("started detached pid" in n.text for n in answered.notes)

    @pytest.mark.skipif(os.name != "nt", reason="creationflags are Windows-only")
    def test_the_detached_child_cannot_open_a_console_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DETACHED_PROCESS makes Windows ignore CREATE_NO_WINDOW: the launcher then
        # has no console, and the python it starts allocates a visible one — a
        # terminal tab that steals focus on every run_now, and on every test run.
        from awask import card_recipes

        seen: dict = {}
        monkeypatch.setattr(card_recipes.subprocess, "Popen",
                            lambda argv, **kw: seen.update(kw))
        card_recipes._detached(["x"])
        flags = seen["creationflags"]
        assert flags & 0x08000000, "CREATE_NO_WINDOW missing"
        assert not flags & 0x00000008, "DETACHED_PROCESS cancels CREATE_NO_WINDOW"

    def test_expiry_applies_the_default_without_spawning(
        self, tmp_path: Path, marker: Path, fake_awrise: Path,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path, monkeypatch)
        card = build_card("wake-failed", VARS)
        card.deadline = time.time() - 1
        stored = store.create(card)
        listed = store.list(status=None)
        expired = next(c for c in listed if c.id == stored.id)
        assert expired.status == "expired"
        assert expired.answer == "keep"
        assert not marker.exists()
        # The three assertions above are satisfied by the expiry transition ALONE:
        # it sets status and answer with no recipe involvement, and "keep" maps to
        # a None steerback, so no recipe could ever spawn on this path. Deleting
        # `self._apply_card_recipe(card)` from `_expire_if_due` left all three
        # green, i.e. the contract-required half of "the hook runs in BOTH
        # transitions" was pinned by nothing. The note is the only observable the
        # hook leaves behind on a no-op answer, so it is what proves it ran.
        reread = store.get(stored.id)
        assert reread is not None
        assert any("Steerback" in n.text for n in reread.notes), (
            "the expiry transition did not route through the card-recipe hook"
        )

    def test_a_broken_action_never_undoes_the_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWRISE_BIN", str(tmp_path / "nope"))
        store = _store(tmp_path, monkeypatch)
        card = store.create(build_card("wake-failed", VARS))
        answered = store.answer(card.id, "disable", via="test", deliver=False)
        assert answered.status == "answered"
        assert answered.answer == "disable"
        reread = store.get(card.id)
        assert reread is not None and reread.status == "answered"
        assert any("not found" in n.text for n in reread.notes)


# ── the CLI door ──────────────────────────────────────────────────────────────


class TestCli:
    def _run(self, argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str]:
        from awask.cli import main

        code = main(argv)
        return code, capsys.readouterr().out

    def test_ask_card_recipe_prints_the_id_and_dedupes_on_the_second_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        import json

        _store(tmp_path, monkeypatch)
        monkeypatch.setenv("AITHER_DECISIONS_NOTIFY", "none")
        argv = ["ask", "--card-recipe", "wake-failed",
                "--var", "job=nightly-sync",
                "--var", "first_failure_ts=2026-09-18T05:00:01+00:00",
                "--var", "n=3", "--json", "--quiet"]
        code, out = self._run(argv, capsys)
        assert code == 0
        first = json.loads(out)
        assert first["deduped"] is False and first["id"]

        code, out = self._run(argv, capsys)
        assert code == 0
        second = json.loads(out)
        assert second["deduped"] is True
        assert second["id"] == first["id"]

    def test_missing_vars_exit_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        _store(tmp_path, monkeypatch)
        code, _ = self._run(["ask", "--card-recipe", "wake-failed",
                             "--var", "job=nightly-sync", "--json"], capsys)
        assert code == 2

    def test_a_var_with_no_equals_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        _store(tmp_path, monkeypatch)
        code, _ = self._run(["ask", "--card-recipe", "wake-failed",
                             "--var", "job", "--json"], capsys)
        assert code == 2

    @pytest.mark.parametrize("clash", [["--option", "a|A|c"], ["--credential"],
                                       ["--recipe", "cloudflare"]])
    def test_card_recipe_is_mutually_exclusive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture, clash: list[str]
    ) -> None:
        _store(tmp_path, monkeypatch)
        code, _ = self._run(["ask", "--card-recipe", "wake-failed",
                             "--var", "job=x", "--var", "n=1",
                             "--var", "first_failure_ts=t", *clash], capsys)
        assert code == 2

    def test_an_unsafe_job_name_never_reaches_the_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        store = _store(tmp_path, monkeypatch)
        code, _ = self._run(["ask", "--card-recipe", "wake-failed",
                             "--var", "job=x; rm -rf /", "--var", "n=1",
                             "--var", "first_failure_ts=2026-09-18T05:00:01+00:00"], capsys)
        assert code == 2
        assert store.list(status=None) == []


# ── the packaging accident, made loud ─────────────────────────────────────────


class TestAMissingRecipeLayerIsNotSilent:
    """A wheel built without ``card_recipes.py`` must not look healthy.

    Both consumers import it LAZILY inside handlers that swallow the failure:
    ``_dedupe_prefix`` returned "" (dedupe guard 2 silently off) and
    ``_apply_card_recipe`` printed one stderr line and returned (answers
    recorded, nothing ever run). So a clean-checkout install passed every local
    check and did nothing in production — the failure mode was silent by
    construction, not by accident. It is a supply-chain-shaped bug: the store IS
    tracked and imports a module that is not.
    """

    def _break_the_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # None in sys.modules is exactly what a missing submodule looks like to
        # `from X import Y`: ImportError, not AttributeError.
        import sys

        monkeypatch.setitem(sys.modules, "awask.card_recipes", None)

    def test_the_failure_lands_on_the_card_not_only_on_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        store = _store(tmp_path, monkeypatch)
        card = store.create(build_card("wake-failed", VARS))
        self._break_the_import(monkeypatch)

        answered = store.answer(card.id, "disable", via="test", deliver=False)

        # The answer still stands — a failed action must never undo it.
        assert answered.status == "answered"
        assert answered.answer == "disable"
        # …and the owner can SEE that nothing ran, on the surface they answered
        # from, rather than being left to believe the wake was disabled.
        reread = store.get(card.id)
        assert reread is not None
        assert any("Steerback FAILED" in n.text for n in reread.notes), (
            "the answer was recorded and nothing ran, with no trace on the card"
        )
        assert any("card_recipes is missing" in n.text for n in reread.notes)

    def test_dedupe_guard_2_says_it_is_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        store = _store(tmp_path, monkeypatch)
        self._break_the_import(monkeypatch)
        store.create(build_card("wake-failed", VARS))
        err = capsys.readouterr().err
        assert "card_recipes is MISSING" in err, (
            "guard 2 was disabled without a word — an installed user's cards would "
            "dedupe on the exact key only"
        )
        # Guard 1 (the exact key) needs nothing from the recipe layer and must
        # keep working, or the missing module would become a card storm.
        second = store.create(build_card("wake-failed", VARS))
        assert len(list((tmp_path / "cards").glob("d-*.json"))) == 1
        assert second.id
