"""Drives the real window with a fake test engine: checks the wiring, not the look. Run: /usr/bin/python3 tests/gui_smoke.py"""
import re, sys, tempfile, threading, time, types
sys.modules["numpy"] = types.ModuleType("numpy")      # the system Python has Tk but no numpy; the window code does not use it
sys.path.insert(0, "src"); sys.argv = ["dantetest"]
import tkinter as tk
import dantetest as d

d.devices = lambda kind: [(0, {"name": "Speaker (Realtek(R) Audio)" if kind == "output" else "Microphone Array (AMD Audio Device)"}),
                          (1, {"name": "Speakers (Dante USB I/O Module)" if kind == "output" else "Microphone (Dante USB I/O Module)"})]
d.dante_devices = lambda: [{"name": n, "usb": False, "rx": {1: "1", 2: "2"}} for n in ("AVIO-A", "AVIO-B", "Amp-1")]
d.na_json = lambda *a: [{"rx_device": "Amp-1", "rx_channel_number": 1, "tx_device": "Desk"}]   # Amp-1 input 1 is in use
d.amd_state = lambda: "disabled"
d.WINDOWS = True                      # show the Windows-only AMD label and full-test button
PHASE = {"label": "AMD USB Audio Device DISABLED", "start": "t0", "end": "t1", "stalls": 0, "stall_s": 0, "listened_s": 60, "tone_s": 50,
         "ever_received": True, "dropouts": 1, "dropout_s": 9.5, "longest_dropout_s": 9.5, "glitch_blocks": 0,
         "driver_errors_in": 0, "driver_errors_out": 0}
seen = []

class FakeSession:
    def __init__(self, out, st, sink, on_status, gate=None, peer=None):
        self.st, self.sink, self.on_status, self.stop, self.log = st, sink, on_status, threading.Event(), sink
    def run(self):
        self.sink("12:00:00.000  TEST_START     fake")
        base = {"level": -15.1, "dropouts": 0, "dropout_s": 0.0, "glitches": 0, "driver_errors": 0, "elapsed": 61, "left": 239, "label": PHASE["label"]}
        for state in ("waiting", "ok", "dropout"):
            self.on_status({**base, "state": state}); time.sleep(0.3)
        self.st["phases"].append(PHASE)
        return "done"
d.Session = FakeSession
sent = []
theirs = {}
d.Peers = lambda on_command, state, settings=None: types.SimpleNamespace(others=lambda: [('OtherPC', '10.0.0.2', 'ready')], command=lambda what, **kw: sent.append((what, kw)), newest_settings=lambda: theirs or None, heard=lambda: None)   # never talk to real PCs from a test

root = tk.Tk()
args = types.SimpleNamespace(device="Dante", no_link=False, minutes=None, out=tempfile.mkdtemp(), cont=None, exclusive=False, signal="tone", record=False)
app = d.App(root, args)
errors = []
app.messagebox = types.SimpleNamespace(showerror=lambda t, m: errors.append(m), askokcancel=lambda t, m: True)

def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:                      # Apple's old Tk blocks in update(), so drain the app's queue directly
            while True:
                app.handle(*app.q.get_nowait())
        except d.queue.Empty:
            pass
        root.update_idletasks(); time.sleep(0.02)
        seen.append(app.status["text"])

pump(0.6)
assert app.cb_out.get() == "Speakers (Dante USB I/O Module)" and app.cb_in.get() == "Microphone (Dante USB I/O Module)", (app.cb_out.get(), app.cb_in.get())
assert app.amd_var.get().endswith("disabled") and str(app.btn_full["state"]) == "normal", app.amd_var.get()
assert list(app.cb_a["values"]) == ["AVIO-A", "AVIO-B", "Amp-1"] and app.dante_var.get().startswith("3 Dante devices"), app.dante_var.get()
assert list(app.cb_mon["values"])[-2:] == ["Amp-1 input 2", "AVIO-B input 2"][:1] + ["AVIO-B input 2"] or "Amp-1 input 1" not in app.cb_mon["values"], app.cb_mon["values"]
assert "Amp-1 input 2" in app.cb_mon["values"] and "Amp-1 input 1" not in app.cb_mon["values"], app.cb_mon["values"]   # never offer an input in use
app.start(False)
assert len(errors) == 1 and "Choose which one" in errors[0], errors          # 3 adapters and none chosen: must ask
app.cb_a.set("AVIO-B"); app.cb_b.set("Amp-1"); app.minutes.set("5")
app.start(False)
assert str(app.btn_mon["state"]) == "disabled" and str(app.btn_stop["state"]) == "normal"
pump(1.5)
st = app.session.st
assert st["adapters"] == ["AVIO-B", "Amp-1"] and st["minutes"] == 5 and st["link"] and not st["matrix"], st
assert st["device_in"] == re.escape("Dante USB I/O Module") == st["device_out"], st
assert re.search(st["device_in"], "Dante USB Receive (Dante USB I/O Module)")   # still matches after Windows renames the input
text = app.text.get("1.0", "end")
assert "TEST_START" in text and "RESULT: FAIL - 1 dropout(s) totalling 9.5 s" in text, text
assert app.status["text"] == "Finished - FAIL (see below)" and str(app.btn_mon["state"]) == "normal", app.status["text"]
cells = [w["text"] for w in app.table.winfo_children()]
assert "FAIL: 1 dropout(s), 9.5 s" in cells and cells[0] == "Check" and app.table.winfo_manager() == "grid", cells   # the table of what failed
assert sent == [("start", {"signal": "tone", "minutes": 5.0})], sent              # Start here is passed to the other PC
app.poll()
assert app.peer_var.get().startswith("Found OtherPC"), app.peer_var.get()
app.handle("peer", {"t": "start", "host": "OtherPC", "signal": "music", "minutes": 2})   # and Start there starts a test here
assert app.signal.get() == "music" and app.minutes.get() == "2" and len(sent) == 1 and str(app.btn_stop["state"]) == "normal"
pump(1.5)
theirs.update(ver=99, signal="speech", minutes="7", a="AVIO-A", b="AVIO-B", hear=True, src=d.TURN, mon="Amp-1 input 2")   # the other PC changes its settings
app.poll()
assert (app.signal.get(), app.minutes.get(), app.cb_a.get(), app.cb_b.get(), app.hear.get(), app.cb_mon.get()) == ("speech", "7", "AVIO-B", "AVIO-A", True, "Amp-1 input 2")
print("status sequence:", list(dict.fromkeys(seen)))
print("counters:", app.counters.get())
print("ok gui smoke")
root.destroy()
