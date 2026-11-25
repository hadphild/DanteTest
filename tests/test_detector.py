"""Self-check for the dropout/glitch detector. Run: .venv/bin/python tests/test_detector.py"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import dantetest  # noqa: E402
from dantetest import ALL, AMP, FREQ, SR, Detector, Session, heard_by_peer, make_signal, results_table, summary  # noqa: E402


def tone(seconds, phase=0.0):
    n = np.arange(int(seconds * SR))
    return (AMP * np.sin(2 * np.pi * FREQ * n / SR + phase)).astype(np.float32)


def run(x, block=441, what="tone"):   # odd block size on purpose: windows must not depend on block boundaries
    events = []
    d = Detector(events.append, what=what)
    for i in range(0, len(x), block):
        d.feed(x[i:i + block])
    return d.finish(), events


def test_clean_tone():
    s, ev = run(tone(5))
    assert s["ever_received"] and s["dropouts"] == 0 and s["glitch_blocks"] == 0, (s, ev)


def test_dropout_to_digital_zero():
    s, ev = run(np.concatenate([tone(3), np.zeros(2 * SR, np.float32), tone(3)]))
    assert s["dropouts"] == 1 and abs(s["dropout_s"] - 2.0) < 0.15 and s["glitch_blocks"] == 0, (s, ev)
    assert any("digital silence" in e for e in ev), ev


def test_dropout_still_open_at_end():
    s, ev = run(np.concatenate([tone(3), np.zeros(2 * SR, np.float32)]))
    assert s["dropouts"] == 1 and abs(s["dropout_s"] - 2.0) < 0.15, (s, ev)


def test_phase_jump_is_a_glitch_not_a_dropout():
    s, ev = run(np.concatenate([tone(2.513), tone(2.5, phase=2.0)]))
    assert s["dropouts"] == 0 and s["glitch_blocks"] >= 1, (s, ev)


def test_silence_only():
    s, ev = run(np.zeros(3 * SR, np.float32))
    assert not s["ever_received"] and s["dropouts"] == 0, (s, ev)


def test_speech_and_music_are_clean_and_a_gap_is_one_dropout():
    for what in ("voice", "music"):   # voice uses this computer's built-in voice
        sig = make_signal(what)
        assert len(sig) > 5 * SR, (what, len(sig))
        x = np.tile(sig, 3)[1234:] * 0.7   # the receiver's windows never line up with the sender's
        s, ev = run(x, what=what)
        assert s["ever_received"] and s["dropouts"] == 0 and s["glitch_blocks"] == 0, (what, s, ev)
        s, ev = run(np.concatenate([x[:4 * SR], np.zeros(2 * SR, np.float32), x[:4 * SR]]), what=what)
        assert s["dropouts"] == 1 and abs(s["dropout_s"] - 2.0) < 0.2, (what, s, ev)


def test_results_table_and_what_the_other_pc_heard():
    live = lambda run, on, n, sec: ("OtherPC", {"id": run, "running": on, "arrived": True, "dropouts": n, "dropout_s": sec})
    assert heard_by_peer(None, None) is None
    assert heard_by_peer(live(1, False, 2, 9), live(1, False, 2, 9)) is None                 # its old, finished test: not measuring now
    assert heard_by_peer(live(1, True, 2, 9.0), live(1, True, 3, 49.5)) == {"host": "OtherPC", "arrived": True, "dropouts": 1, "dropout_s": 40.5}
    assert heard_by_peer(live(1, False, 2, 9), live(2, True, 0, 0))["dropouts"] == 0         # a new test there starts from zero
    base = {"start": "t0", "end": "t1", "ever_received": True, "dropouts": 0, "dropout_s": 0, "longest_dropout_s": 0, "glitch_blocks": 0,
            "listened_s": 60, "tone_s": 60, "stalls": 0, "stall_s": 0, "driver_errors_in": 0, "driver_errors_out": 0, "link_changes": 0, "usb_changes": None}
    st = {"phases": [{**base, "label": "AMD DISABLED", "ever_received": False},
                     {**base, "label": "AMD ENABLED", "heard": {"host": "OtherPC", "arrived": True, "dropouts": 1, "dropout_s": 40.5}}],
          "effects": ["Speakers (X): THIRD-PARTY effect 'A' (a.dll)"]}
    heads, rows = results_table(st)
    table = {name: [c[0] for c in cells] for name, cells in rows}
    assert heads == ["Check", "AMD DISABLED, tone", "AMD ENABLED, tone"], heads
    assert table["The other PC's signal arrived here"] == ["FAIL: nothing received", "OK"], table
    assert table["... and stayed, with no dropouts"] == ["-", "OK"] and table["USB power stayed on"] == ["-", "-"], table
    assert table["This PC's playback arrived at the other PC"] == ["-", "FAIL: lost 1 time(s), 40.5 s"], table
    assert table["Audio processing on the Dante device"][0].startswith("WARNING: third-party"), table
    assert "FAIL: lost 1 time(s), 40.5 s" in summary(st)


def test_music_then_voice_then_tone_and_following_the_other_pc():
    import tempfile, threading
    ran, gates, told, real = [], [], [], dantetest.run_test
    leader = {"step": None}

    def fake_test(log, st, label, stop, on_status, rec_dir=None, peer=None, sig=None, minutes=-1, until=None, before_tail=None):
        ran.append((sig, minutes))
        if before_tail:
            before_tail()
        leader["step"] = {"music#1": "voice#2", "voice#2": "end#3", "end#3": "finished#3"}.get(leader["step"])   # the other PC moves on
        return {"signal": sig, "label": label}
    dantetest.run_test = fake_test
    try:
        def session(**st):
            state = {"phases": [], "minutes": 2, "record": False, **st}
            return Session(Path(tempfile.mkdtemp()), state, lambda line: None, lambda s: None, lambda log, stop, step: gates.append(step),
                           announce=told.append, leader=lambda: leader["step"])
        session(signal=ALL).sequence("X")
        assert ran == [("music", 2), ("voice", 2), ("tone", 2)], ran
        assert gates == ["music#1", "voice#2", "tone#3"] and told == ["voice#2", "tone#3", "end#4"], (gates, told)
        ran.clear(); gates.clear(); told.clear()
        session(signal="tone", minutes=None).sequence("X")                  # one signal, until stopped
        assert ran == [("tone", None)] and told == ["end#2"], (ran, told)
        ran.clear(); gates.clear(); told.clear(); leader["step"] = "music#1"
        follower = session(signal=ALL, follow=True)
        threading.Timer(1.0, lambda: leader.update(step="finished#3")).start()   # while it waits at 'end', the other PC finishes
        follower.follow("X")
        assert ran == [("music", None), ("voice", None)] and gates == ["music#1", "voice#2"], (ran, gates)
    finally:
        dantetest.run_test = real


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
