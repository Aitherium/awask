"""awask's channel loader accepts a relay block, like adk's (#12228)."""
from __future__ import annotations

import json

from awask import channels as ch


def test_relay_entry_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path))
    (tmp_path / "channels.json").write_text(json.dumps({
        "discord": {"enabled": True, "owner_user_id": "1"},
        "relay": {"enabled": True, "owner_user_id": "david", "require_direct_message": False},
    }), encoding="utf-8")
    assert sorted(ch.load_config()) == ["discord", "relay"]


def test_in_step_with_adk():
    try:
        from adk.decisions import channels as adk_ch
    except ImportError:
        return
    assert set(ch.SUPPORTED) == set(adk_ch.SUPPORTED)
