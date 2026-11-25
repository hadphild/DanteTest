#!/usr/bin/env python3
"""DanteTest - two-PC audio test for Dante USB adapters.

Run the same program on both PCs. Each PC plays a continuous test signal (a 1 kHz tone,
or speech or music) into its Dante adapter and listens for the other PC's, so both directions
are tested at once. Every dropout, glitch, audio-driver error and Dante link change
is written to a timestamped log in the results folder.
"""
import os
import sys

if len(sys.argv) > 2 and sys.argv[1] == "_netaudio":
    # Internal: the program re-runs itself as the netaudio command line, output to the file in argv[2].
    sys.stdout = open(sys.argv[2], "w", encoding="utf-8")
    sys.stderr = open(os.devnull, "w")
    sys.argv = ["netaudio"] + sys.argv[3:]
    if os.name == "nt":
        # netaudio starts a console helper program; without this it opens a black window over the test window
        import subprocess as _sp

        class _QuietPopen(_sp.Popen):
            def __init__(self, *a, **k):
                k["creationflags"] = k.get("creationflags", 0) | 0x08000000   # CREATE_NO_WINDOW
                super().__init__(*a, **k)
        _sp.Popen = _QuietPopen
    from netaudio.cli import main as _na_main
    sys.exit(_na_main())

import argparse
import datetime as dt
import json
import platform
import queue
import re
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

VERSION = "1.13"
SR = 48000                      # Dante adapters run at 48 kHz
FREQ = 1000                     # test tone; SR/FREQ must be a whole number of samples
AMP = 0.25                      # -12 dBFS
WIN = SR // 20                  # 50 ms analysis window
SILENCE = 10 ** (-50 / 20)      # RMS below -50 dBFS = no tone
AMD_ID = r"ACP\DEVTYPE_0007&DEVREV_0000&VEN_1022&DEV_15E2"   # AMD USB Audio (offload) Device
TASK = "DanteTestResume"
WINDOWS = platform.system() == "Windows"
FROZEN = getattr(sys, "frozen", False)
NO_WINDOW = 0x08000000 if WINDOWS else 0   # stop child processes flashing a console window
HOST = socket.gethostname()
SIGNALS = ("tone", "speech", "music")
SPEECH = ("The birch canoe slid on the smooth planks. Glue the sheet to the dark blue background. "
          "These days a chicken leg is a rare dish. Rice is often served in round bowls. "
          "The juice of lemons makes fine punch. The box was thrown beside the parked truck.")


def db(x):
    return 20 * np.log10(max(float(x), 1e-10))


class Log:
    def __init__(self, path, sink):
        self.f = open(path, "a", encoding="utf-8", buffering=1)
        self.sink, self.lock, self.counts = sink, threading.Lock(), {}

    def __call__(self, msg, echo=True):
        kind = " ".join(msg.split()[:2]) if msg.startswith("USB_POWER") else msg.split(None, 1)[0]
        self.counts[kind] = self.counts.get(kind, 0) + 1
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with self.lock:
            self.f.write(f"{stamp}  {msg}\n")
        if echo:
            self.sink(f"{stamp[11:]}  {msg}")


class Detector:
    """Feed mono float blocks; logs when the tone appears, drops out, returns or glitches."""

    def __init__(self, log, sr=SR, freq=FREQ, what="tone"):
        self.log, self.sr, self.what = log, sr, what
        tone = what == "tone"
        self.period = sr // freq if tone and sr % freq == 0 else 0   # 0 = glitch check off
        self.hold = 1 if tone else 8    # speech and music dip briefly: a dropout is 8 quiet windows (0.4 s) in a row
        self.bad_run = 0
        self.buf = np.zeros(0, np.float32)
        self.tail = np.zeros(self.period, np.float32)
        self.state = "waiting"      # waiting (never heard the tone) -> ok <-> dropout
        self.good_run = 0           # consecutive windows with tone
        self.pending = 0            # glitch seen last window; counts only if the tone carries on
        self.sec_glitch = self.sec_win = 0
        self.level = self.now = -200.0
        self.total_s = self.tone_s = self.dropout_s = self.longest = self.drop_t = 0.0
        self.dropouts = self.glitches = 0

    def feed(self, x):
        self.buf = np.concatenate([self.buf, np.asarray(x, np.float32)])
        while len(self.buf) >= WIN:
            self._window(self.buf[:WIN])
            self.buf = self.buf[WIN:]

    def _window(self, w):
        win_s = len(w) / self.sr
        rms = float(np.sqrt(np.mean(w * w)))
        good = rms > SILENCE
        self.now = db(rms)
        self.total_s += win_s
        self.good_run = self.good_run + 1 if good else 0
        self.bad_run = 0 if good else self.bad_run + 1

        # A sine repeats every `period` samples, so x[n] != x[n-period] means damaged audio.
        # Needs 2 good windows before it and 1 after, so dropout edges are not counted.
        glitch = 0
        if self.period and good and self.good_run >= 3:
            ext = np.concatenate([self.tail, w])
            glitch = int(np.any(np.abs(ext[self.period:] - ext[:-self.period]) > 0.3 * rms * 1.414))
        if good and self.pending:
            self.glitches += 1
            self.sec_glitch += 1
        self.pending = glitch
        if self.period:
            self.tail = w[-self.period:].copy()
        self.sec_win += 1
        if self.sec_win >= 20:
            if self.sec_glitch:
                self.log(f"GLITCH         damaged audio in {self.sec_glitch} of the last 20 blocks (50 ms each)")
            self.sec_glitch = self.sec_win = 0

        if self.state == "waiting":
            if self.good_run >= 10:
                self.state, self.level = "ok", db(rms)
                self.log(f"SIGNAL_OK      {self.what} received from the other PC, level {db(rms):.1f} dBFS")
        elif self.state == "ok":
            if good:
                self.tone_s += win_s
                self.level = db(rms)
            elif self.bad_run >= self.hold:
                self.state, self.drop_t = "dropout", self.total_s - self.hold * win_s
                self.dropouts += 1
                kind = "digital silence, every sample exactly zero" if not np.any(w) else f"level fell to {db(rms):.0f} dBFS"
                self.log(f"DROPOUT_START  {self.what} lost ({kind})")
        elif self.good_run >= 4:
            dur = self.total_s - 4 * win_s - self.drop_t
            self._close_dropout(dur)
            self.state, self.level = "ok", db(rms)
            self.log(f"DROPOUT_END    {self.what} back after {dur:.2f} s")

    def _close_dropout(self, dur):
        self.dropout_s += dur
        self.longest = max(self.longest, dur)

    def dropout_now(self):
        return self.dropout_s + (self.total_s - self.drop_t if self.state == "dropout" else 0)

    def finish(self):
        if self.state == "dropout":
            dur = self.total_s - self.drop_t
            self._close_dropout(dur)
            self.log(f"DROPOUT_OPEN   {self.what} still missing at the end of the test ({dur:.2f} s)")
        return {"listened_s": round(self.total_s, 1), "tone_s": round(self.tone_s, 1), "ever_received": self.state != "waiting",
                "dropouts": self.dropouts, "dropout_s": round(self.dropout_s, 2), "longest_dropout_s": round(self.longest, 2),
                "glitch_blocks": self.glitches}


# ---------- audio ----------

def speak(path):
    """Speech from the PC's own built-in voice, so nothing has to be shipped with the program."""
    if WINDOWS:
        ps("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
           f"$s.SetOutputToWaveFile('{path}'); $s.Speak('{SPEECH}'); $s.Dispose()")
    else:
        subprocess.run(["say", "-o", path, "--data-format=LEI16@48000", SPEECH], capture_output=True)


def make_signal(kind):
    """One loop of the test signal, mono float32. Speech and music have their pauses cut out, so a
    quiet stretch at the other end always means a fault."""
    import wave
    t = np.arange(2 * SR) / SR
    if kind == "tone":
        return (AMP * np.sin(2 * np.pi * FREQ * t[:SR])).astype(np.float32)
    if kind == "music":   # synthesised: a plucked tune over held chords (A, F, C, G)
        fade = np.minimum(1, np.minimum(t, 2 - t) / 0.02)
        beat = t[:SR // 2]
        bars = []
        for chord in ((220, 277.18, 329.63), (174.61, 220, 261.63), (130.81, 164.81, 196), (196, 246.94, 293.66)):
            held = sum(np.sin(2 * np.pi * f * t) for f in chord) / 3
            tune = np.concatenate([sum(np.sin(2 * np.pi * 2 * f * h * beat) / h for h in (1, 2, 3)) * np.exp(-5 * beat)
                                   for f in chord + chord[1:2]])
            bars.append((0.4 * held + 0.6 * tune) * fade)
        x = np.concatenate(bars)
    else:
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            speak(path)
            with wave.open(path) as w:
                rate, ch = w.getframerate(), w.getnchannels()
                x = np.frombuffer(w.readframes(w.getnframes()), np.int16)[::ch].astype(np.float64)
        except Exception as e:
            raise RuntimeError(f"could not make the speech signal with this PC's built-in voice ({e})") from e
        finally:
            os.unlink(path)
        if len(x) < rate:
            raise RuntimeError("could not make the speech signal: this PC's built-in voice produced no audio")
        x = np.interp(np.arange(0, len(x), rate / SR), np.arange(len(x)), x)   # to 48 kHz
    x = x / np.max(np.abs(x)) * 0.5                       # peaks at -6 dBFS
    w = x[:len(x) // WIN * WIN].reshape(-1, WIN)
    return w[np.sqrt((w * w).mean(1)) > 0.01].ravel().astype(np.float32)   # drop windows quieter than -40 dBFS


def devices(kind):
    """[(index, info)] for input or output devices (WASAPI only on Windows, so each appears once)."""
    import sounddevice as sd
    wasapi = next((i for i, a in enumerate(sd.query_hostapis()) if "WASAPI" in a["name"]), None)
    return [(i, d) for i, d in enumerate(sd.query_devices())
            if d[f"max_{kind}_channels"] > 0 and (wasapi is None or d["hostapi"] == wasapi)]


def pick_device(pattern, kind):
    """First device matching the pattern. 'A|B' means prefer a device matching A, and fall back to B."""
    found = devices(kind)
    for alt in re.split(r"(?<!\\)\|", pattern):
        hit = next((c for c in found if re.search(alt, c[1]["name"], re.I)), None)
        if hit:
            return hit


def pattern_for(name):
    """Windows renames 'Microphone (Dante USB I/O Module)' to 'Dante USB Receive (...)' when the AMD
    device is switched, so match on the part in brackets, which stays the same."""
    m = re.search(r"\(([^()]+)\)\s*$", name)
    return re.escape(m.group(1) if m else name)


def reinit_audio():
    import sounddevice as sd
    sd._terminate()
    sd._initialize()


class Audio:
    """Plays the test signal and captures the input; recover() copes if the input stalls or Windows pulls the device."""

    def __init__(self, in_pat, out_pat, log, exclusive=False, signal=None):
        self.in_pat, self.out_pat, self.log, self.exclusive = in_pat, out_pat, log, exclusive
        self.sig = make_signal("tone") if signal is None else signal
        self.q = queue.Queue()
        self.xrun = {"input": 0, "output": 0}
        self.streams, self.found, self.names = {}, {}, None
        self.phase = 0

    def _out(self, outdata, frames, t, status):
        if status:
            self.xrun["output"] += 1
        outdata[:] = self.sig[(self.phase + np.arange(frames)) % len(self.sig)][:, None]
        self.phase = (self.phase + frames) % len(self.sig)

    def _in(self, indata, frames, t, status):
        if status:
            self.xrun["input"] += 1
        self.q.put(indata[:, 0].copy())

    def _start(self, kind):
        import sounddevice as sd
        pat = self.in_pat if kind == "input" else self.out_pat
        dev = pick_device(pat, kind)
        if not dev:
            raise RuntimeError(f"audio {kind} device not found (looking for '{pat}')")
        # WASAPI exclusive mode talks to the driver directly: no Windows mixer, no effects (APOs)
        extra = sd.WasapiSettings(exclusive=True) if self.exclusive and WINDOWS else None
        make, callback = (sd.InputStream, self._in) if kind == "input" else (sd.OutputStream, self._out)
        try:
            stream = make(device=dev[0], channels=min(2, dev[1][f"max_{kind}_channels"]), samplerate=SR,
                          dtype="float32", callback=callback, extra_settings=extra)
        except sd.PortAudioError as e:
            raise RuntimeError(f"{kind}: {e} - check the Dante device is set to 48000 Hz in the sound settings") from e
        stream.start()
        self.streams[kind] = stream
        self.found[kind] = dev[1]["name"]

    def _stop(self, kind):
        stream = self.streams.pop(kind, None)
        try:
            stream and stream.close()
        except Exception:
            pass

    def open(self):
        self._start("input")
        self._start("output")
        names = (self.found["input"], self.found["output"])
        if names != self.names:
            self.log(f"AUDIO_DEVICES  playing to '{names[1]}', listening on '{names[0]}'")
            self.names = names

    def close(self):
        self._stop("input")
        self._stop("output")

    def recover(self):
        """The input stopped delivering. Reopen only the input, so the tone this PC plays is not interrupted;
        restart everything only if playback has died too (the device was unplugged or re-enumerated)."""
        self._stop("input")
        out = self.streams.get("output")
        try:
            if not (out and out.active):
                raise RuntimeError("playback stopped")
            self._start("input")
        except Exception:
            self.close()
            reinit_audio()
            self.open()


def save_wav(path, x=None):
    """Open a 48 kHz mono 16-bit WAV; with `x`, write it and close."""
    import wave
    w = wave.open(str(path), "wb")
    w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
    if x is None:
        return w
    w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    w.close()


def heard_by_peer(before, after):
    """What the other PC's DanteTest received from this PC during a test, from its counters at the start and end.
    None if no other DanteTest was measuring."""
    if not after or not after[1]:
        return None
    host, b = after
    a = before[1] if before and before[1] else None
    if a and a.get("id") == b.get("id") and not a.get("running"):
        return None                                    # the same finished test as before: it was not running during this one
    if not a or a.get("id") != b.get("id"):
        a = {"dropouts": 0, "dropout_s": 0}
    return {"host": host, "arrived": bool(b.get("arrived")), "dropouts": b.get("dropouts", 0) - a["dropouts"],
            "dropout_s": round(b.get("dropout_s", 0) - a["dropout_s"], 1)}


def run_test(log, st, label, stop, on_status, rec_dir=None, peer=lambda: None):
    """One test phase. Returns the stats dict. Setting `stop` ends it early.
    With `rec_dir`, the signal played and everything received are saved there as WAV files."""
    minutes, sig = st["minutes"], st.get("signal") or "tone"
    log(f"TEST_START     {label}" + (f", {minutes:g} minutes" if minutes else ", until stopped")
        + (f", {sig} test signal" if sig != "tone" else "")
        + (", EXCLUSIVE mode (Windows audio effects bypassed)" if st.get("exclusive") else ""))
    det = Detector(log, what=sig)
    played = make_signal(sig)
    audio = Audio(st["device_in"], st["device_out"], log, st.get("exclusive"), played)
    rec = None
    if rec_dir:
        # ponytail: records the whole test, about 6 MB a minute; cap or split it if long recorded runs are needed
        name = f"received-{sig}-{dt.datetime.now():%H%M%S}.wav"
        save_wav(Path(rec_dir) / f"played-{sig}.wav", played)
        rec = save_wav(Path(rec_dir) / name)
        log(f"RECORDING      saving the audio received to {name}; the signal this PC plays is in played-{sig}.wav (one loop)")
    stats = {"label": label, "signal": sig, "start": dt.datetime.now().isoformat(timespec="seconds"), "stalls": 0, "stall_s": 0.0}
    counts0, peer0 = dict(log.counts), peer()
    t0 = time.time()
    end = t0 + minutes * 60 if minutes else None
    last_block = last_tick = last_beat = t0
    stall_t, warned, nosig, seen = None, False, False, {"input": 0, "output": 0}
    flowed = counted = False   # some adapters need the input reopened once at the start; that is not counted as a fault

    def reopen():
        nonlocal warned
        try:
            audio.recover() if audio.streams else audio.open()
        except Exception as e:
            if not warned:
                log(f"AUDIO_ERROR    {e}")
                warned = True

    if WINDOWS:
        reinit_audio()   # Windows audio must be set up on the thread that uses it; the window has already used it on another
    reopen()
    try:
        while not stop.is_set() and (end is None or time.time() < end):
            try:
                block = audio.q.get(timeout=0.25)
                det.feed(block)
                if rec:
                    rec.writeframes((np.clip(block, -1, 1) * 32767).astype("<i2").tobytes())
                last_block, flowed = time.time(), True
                if stall_t:
                    stats["stall_s"] += (last_block - stall_t) * counted
                    log(f"AUDIO_RESUMED  input device delivering audio again after {last_block - stall_t:.1f} s")
                    stall_t, warned = None, False
            except queue.Empty:
                now = time.time()
                if now - last_block > 2:
                    if not stall_t:
                        stall_t, counted = last_block, flowed
                        stats["stalls"] += counted
                        log("AUDIO_STALLED  the input device is not delivering audio; trying to reopen it")
                    reopen()
                    last_block = now + 1   # retry about every 3 s
            now = time.time()
            if now - last_tick >= 0.5:
                last_tick = now
                for k in seen:
                    if audio.xrun[k] != seen[k]:
                        log(f"AUDIO_DRIVER   {k} buffer error reported by the audio driver x{audio.xrun[k] - seen[k]}")
                        seen[k] = audio.xrun[k]
                if det.state == "waiting" and now - t0 > 30 and not nosig:
                    nosig = True
                    log(f"NO_SIGNAL      no {sig} received after 30 s. Is DanteTest running on the other PC, and is the Dante link up?")
                s = {"state": "stalled" if stall_t else det.state, "level": det.level, "now": det.now, "dropouts": det.dropouts,
                     "dropout_s": det.dropout_now(), "glitches": det.glitches, "driver_errors": sum(audio.xrun.values()),
                     "elapsed": now - t0, "left": end - now if end else None, "label": label}
                on_status(s)
                if now - last_beat >= 60:
                    last_beat = now
                    log("STATUS         " + status_text(s), echo=False)
        if stop.is_set():
            log("STOPPED        by user")
        stats["heard"] = heard_by_peer(peer0, peer())
        stats["link_changes"] = log.counts.get("DANTE_CHANGE", 0) - counts0.get("DANTE_CHANGE", 0) if st.get("adapters") and st["link"] else None
        stats["usb_changes"] = (log.counts.get("USB_POWER CHANGE:", 0) - counts0.get("USB_POWER CHANGE:", 0)) if st.get("usb") else None
        # Keep playing a little longer without listening: the other PC finishes a moment before or after
        # this one, and must not log this PC stopping as a dropout.
        tail = time.time() + (1.5 if stop.is_set() else 3)
        while time.time() < tail:
            try:
                audio.q.get(timeout=0.25)
            except queue.Empty:
                pass
    finally:
        audio.close()
        if rec:
            rec.close()
    if stall_t:
        stats["stall_s"] += (time.time() - stall_t) * counted
    stats.update(det.finish(), end=dt.datetime.now().isoformat(timespec="seconds"),
                 driver_errors_in=audio.xrun["input"], driver_errors_out=audio.xrun["output"], stall_s=round(stats["stall_s"], 1))
    log(f"TEST_END       {label}: {verdict(stats)}")
    return stats


def clock(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def status_text(s):
    state = {"waiting": "waiting for the other PC", "ok": f"receiving OK {s['level']:.1f} dBFS", "dropout": "DROPOUT",
             "stalled": "AUDIO DEVICE STALLED"}[s["state"]]
    return (f"{state} | dropouts {s['dropouts']} ({s['dropout_s']:.1f} s) | damaged blocks {s['glitches']} | "
            f"driver errors {s['driver_errors']} | running {clock(s['elapsed'])}")


def verdict(s):
    what = s.get("signal", "tone")
    if not s["ever_received"]:
        return f"FAIL - no {what} was ever received"
    bad = [f"{s['dropouts']} dropout(s) totalling {s['dropout_s']} s" if s["dropouts"] else "",
           f"damaged audio in {s['glitch_blocks']} block(s)" if s["glitch_blocks"] else "",
           f"input device stalled {s['stalls']} time(s), {s['stall_s']} s" if s["stalls"] else ""]
    bad = [b for b in bad if b]
    return "FAIL - " + "; ".join(bad) if bad else f"PASS - clean {what} for {s['tone_s']} s"


# ---------- Dante link (netaudio) ----------

def self_cmd():
    return [sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)]


NA_LOCK = threading.Lock()   # one Dante command at a time: several threads use it


def na(*args, timeout=120):
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        with NA_LOCK:
                subprocess.run(self_cmd() + ["_netaudio", path, *args], timeout=timeout, env={**os.environ, "NO_COLOR": "1"},
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"error: {e}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def na_json(*args):
    try:
        return json.loads(na("-j", *args))
    except ValueError:
        return None


def dante_devices():
    """Every Dante device on the network: [{name, usb, rx}]. usb = looks like a USB adapter; rx = {input number: label}."""
    out = []
    for key, v in ((na_json("device", "list") or {}).get("dante") or {}).items():
        name = v.get("name", key)
        rx = {int(n): (c.get("name") or n) for n, c in ((v.get("channels") or {}).get("receivers") or {}).items()}
        out.append({"name": name, "usb": "usb" in f"{name} {v.get('model') or ''}".lower(), "rx": rx})
    return sorted(out, key=lambda d: d["name"])


def usb_pair(devs):
    """The two adapters to link when the choice is obvious: the only two devices, or the only two USB adapters."""
    for names in ([d["name"] for d in devs], [d["name"] for d in devs if d["usb"]]):
        if len(names) == 2:
            return names
    return None


def monitor_inputs(devs, subs, pair):
    """Inputs of other Dante devices (a monitor speaker, a mixer) that nothing is patched to, or that one of
    our adapters already feeds: ['Device input N']. Inputs patched to something else are never offered."""
    busy = {(s.get("rx_device"), s.get("rx_channel_number")): s.get("tx_device") for s in subs or [] if s.get("tx_device")}
    return [f"{d['name']} input {n}" for d in devs if not d["usb"] and d["name"] not in (pair or []) for n in sorted(d["rx"])
            if busy.get((d["name"], n)) in (None, *(pair or []))]


TURN = "each adapter in turn"
MONITOR_RE = r"(.+) input (\d+)=(.+)"


def monitor_patch(log, monitor, pair, undo=None):
    """monitor = 'Device input N=Source', Source being an adapter's name or TURN. Patches that adapter's channel 1
    to the input so the test can be heard. Returns how to put the input back afterwards (pass it back as `undo`):
    'remove', the name of the adapter it was listening to before, or None if nothing was changed."""
    m = re.fullmatch(MONITOR_RE, monitor or "")
    if not m:
        return None
    device, n, source = m.group(1), int(m.group(2)), m.group(3)
    rx = f"rx:{n}@{device}"
    if undo == "remove":
        na("subscription", "remove", "--rx", rx)
        return log(f"MONITOR        {device} input {n} unpatched again")
    if undo:
        na("subscription", "add", "--tx", f"tx:1@{undo}", "--rx", rx)
        return log(f"MONITOR        {device} input {n} put back on {undo}")
    if source == TURN and not pair:
        return log("MONITOR        cannot switch between the adapters: the two adapters are not known")
    first = pair[0] if source == TURN else source
    now = next((s.get("tx_device") for s in na_json("subscription", "list") or []
                if s.get("rx_device") == device and s.get("rx_channel_number") == n and s.get("tx_device")), None)
    if now and now not in (pair or [first]):
        return log(f"MONITOR        {device} input {n} is patched to {now}; left alone. Choose a free input.")
    if now != first:
        na("subscription", "add", "--tx", f"tx:1@{first}", "--rx", rx)
    log(f"MONITOR        {device} input {n} now hearing {first}")
    return None if now == first and source != TURN else (now or "remove")


def monitor_follow(log, monitor, pair, stop, seconds=15):
    """Background, for TURN: move the monitor from one adapter to the other every few seconds, so the audio
    sent and the audio received are heard in turn."""
    m = re.fullmatch(MONITOR_RE, monitor)
    device, n, i = m.group(1), int(m.group(2)), 0
    while not stop.wait(seconds):
        i ^= 1
        na("subscription", "add", "--tx", f"tx:1@{pair[i]}", "--rx", f"rx:{n}@{device}")
        if not stop.is_set():
            log(f"MONITOR        {device} input {n} now hearing {pair[i]}")


def link_lines(subs, pair):
    out = {}
    for s in subs or []:
        if s.get("rx_device") in pair:
            out[f"{s['rx_device']} in {s.get('rx_channel_number')}"] = \
                f"<- {s.get('tx_channel') or '-'}@{s.get('tx_device') or '-'}: {(s.get('status') or {}).get('label', '?')}"
    return out


def setup_link(log, pair):
    """Subscribe each of the two Dante adapters to the other. Returns the pair, or None if not set up."""
    log("DANTE          looking for Dante adapters on the network and checking the link (can take a minute or two)...")
    devs = dante_devices()
    names = [d["name"] for d in devs]
    log(f"DANTE          devices found: {', '.join(names) or 'none'}")
    pair = pair or usb_pair(devs)
    if not pair or any(p not in names for p in pair):
        why = "the chosen adapters are not on the network" if pair else f"found {len(names)} Dante devices, need to know which two to link"
        log(f"DANTE          LINK NOT SET UP: {why}. This PC must be on the same network as the adapters. "
            "Set the routing in Dante Controller instead; the audio test carries on.")
        return None
    a, b = pair

    def linked(subs, rx, tx):
        return sum(1 for s in subs or [] if s.get("rx_device") == rx and s.get("tx_device") == tx
                   and (s.get("status") or {}).get("state") == "connected") >= 2

    subs = na_json("subscription", "list")
    for rx, tx in ((a, b), (b, a)):
        if linked(subs, rx, tx):
            log(f"DANTE          {tx} -> {rx} already linked")
        else:
            log(f"DANTE          linking {tx} -> {rx}")
            na("subscription", "add", "--tx", tx, "--rx", rx)
    for _ in range(4):
        subs = na_json("subscription", "list")
        if linked(subs, a, b) and linked(subs, b, a):
            break
        time.sleep(3)
    for k, v in link_lines(subs, pair).items():
        log(f"DANTE_LINK     {k} {v}")
    if not (linked(subs, a, b) and linked(subs, b, a)):
        log("DANTE          WARNING: the link is not fully connected; check Dante Controller")
    return list(pair)


def monitor_link(log, pair, stop):
    """Background: log any change in the Dante subscriptions (each poll takes ~15 s)."""
    last = link_lines(na_json("subscription", "list"), pair)
    while not stop.wait(15):
        subs = na_json("subscription", "list")
        if stop.is_set():
            return
        cur = link_lines(subs, pair) if subs is not None else {}
        for k in sorted(set(last) | set(cur)):
            if last.get(k) != cur.get(k):
                log(f"DANTE_CHANGE   {k} {cur.get(k, 'no longer reported (adapter offline?)')}   (was: {last.get(k, 'not reported')})")
        last = cur


# ---------- system info / AMD device (Windows) ----------

def ps(script, timeout=120):
    try:
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW).stdout.strip()
    except Exception as e:
        return f"error: {e}"


SYSINFO_PS = r"""
$ErrorActionPreference='SilentlyContinue'
$cs=Get-CimInstance Win32_ComputerSystem; $b=Get-CimInstance Win32_BIOS; $bb=Get-CimInstance Win32_BaseBoard
$os=Get-CimInstance Win32_OperatingSystem; $cpu=Get-CimInstance Win32_Processor
"Model:  $($cs.Manufacturer) $($cs.Model) (SKU $($cs.SystemSKUNumber), board $($bb.Product))"
"Serial: $($b.SerialNumber)"
"BIOS:   $($b.SMBIOSBIOSVersion) ($($b.ReleaseDate.ToString('yyyy-MM-dd')))"
"CPU:    $($cpu.Name)"
"OS:     $($os.Caption) build $($os.BuildNumber)"
""; "--- AMD, Dante and USB audio devices"
Get-PnpDevice | ? { $_.InstanceId -like 'ACP\DEVTYPE_0007*' -or $_.FriendlyName -match 'Dante|AMD.*Audio|USB Audio' } |
  sort Class,FriendlyName | ft -auto Status,Problem,Class,FriendlyName,InstanceId | Out-String -Width 260
"--- Drivers"
Get-CimInstance Win32_PnPSignedDriver | ? { $_.DeviceName -match 'Dante|AMD.*Audio|USB Audio' } |
  ft -auto DeviceName,DriverVersion,DriverDate,InfName | Out-String -Width 260
"""


FX_PS = r"""
$ErrorActionPreference='SilentlyContinue'
$lm = [Microsoft.Win32.Registry]::LocalMachine          # .NET registry calls: the PowerShell provider takes a minute here
$base = 'SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio'
foreach ($flow in 'Render','Capture') { $fk = $lm.OpenSubKey("$base\$flow"); foreach ($g in $fk.GetSubKeyNames()) {
  $ek = $fk.OpenSubKey($g); if ($ek.GetValue('DeviceState') -ne 1) { continue }
  $pk = $ek.OpenSubKey('Properties')
  $name = "$($pk.GetValue('{a45c254e-df1c-4efd-8020-67d146a850e0},2')) ($($pk.GetValue('{b3f8fa53-0004-438e-9003-51a46e139bfc},6')))"
  "EP`t$flow`t$name`t$($pk.GetValue('{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5'))`t$g"
  $fx = $ek.OpenSubKey('FxProperties')
  if ($fx) { foreach ($v in $fx.GetValueNames()) { foreach ($one in @($fx.GetValue($v))) {
    if ($one -is [string] -and $one -match '^\{[0-9A-Fa-f-]{36}\}$') {
      $ak = $lm.OpenSubKey("SOFTWARE\Classes\AudioEngine\AudioProcessingObjects\$one"); $apo = if ($ak) { $ak.GetValue('FriendlyName') }
      $ck = $lm.OpenSubKey("SOFTWARE\Classes\CLSID\$one\InprocServer32"); $dll = if ($ck) { $ck.GetValue('') }
      if ($apo -or $dll) { "FX`t$flow`t$name`t$apo`t$dll" } } } } }
} }
$known = 'Waves|Maxx|Dolby|DTS|Nahimic|Sonic Studio|Smart Sound|Studio Effects|Voice|Noise|Audio Effects|Sound Blaster|Bang|Harman|THX|Krisp|Broadcast|Senary|Cirrus'
"SW`t" + ((@(Get-PnpDevice -PresentOnly -Class AudioProcessingObject) + @(Get-PnpDevice -PresentOnly -Class SoftwareComponent | ? FriendlyName -match $known) | % FriendlyName | sort -Unique) -join '; ')
"""
MS_FX = re.compile(r"\\(WMALFXGFXDSP|MsApoFxProxy|SysFxUI)\.dll$", re.I)   # Microsoft's own, inactive unless switched on


def audio_effects(in_pat, out_pat):
    """Lines describing the audio processing Windows has attached to the chosen devices (Windows only)."""
    if not WINDOWS:
        return []
    out, seen = [], set()
    for row in (r.split("\t") for r in ps(FX_PS).splitlines()):
        pat = {"Render": out_pat, "Capture": in_pat}.get(row[1] if len(row) > 1 else "")
        if row[0] == "SW" and row[1:] and row[1]:
            out.append(f"audio processing components installed on this PC: {row[1]}")
        elif pat is None or len(row) < 4 or not re.search(pat, row[2], re.I):
            continue
        elif row[0] == "EP":
            out.append(f"{row[2]}: Windows 'audio enhancements' are {'switched OFF' if row[3] == '1' else 'NOT switched off (WARNING)'}")
        elif row[0] == "FX" and (row[2], row[3], row[4]) not in seen:
            seen.add((row[2], row[3], row[4]))
            third = not MS_FX.search(row[4])
            out.append(f"{row[2]}: {'THIRD-PARTY effect' if third else 'Microsoft effect'} '{row[3] or '?'}' ({row[4].rsplit(chr(92), 1)[-1]})")
    return out


def enhancement_state():
    """Every active sound device: [(flow, name, enhancements on?, device id)]. flow is Render (playback) or Capture."""
    rows = [r.split("\t") for r in ps(FX_PS).splitlines()]
    return [(r[1], r[2], r[3] != "1", "{0.0.%d.00000000}.%s" % (r[1] == "Capture", r[4]))
            for r in rows if r[0] == "EP" and len(r) > 4]


# Windows keeps this setting per sound device. The registry key is read-only even for administrators,
# so it is written through the Windows audio API (the same call the Sound settings page makes).
FXSET_PS = r"""
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
[StructLayout(LayoutKind.Sequential)] public struct PKEY { public Guid fmtid; public int pid; }
[StructLayout(LayoutKind.Explicit, Size = 24)] public struct PV { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public uint u; }
[Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown), ComImport]
public interface IPropertyStore { void GetCount(out int n); void GetAt(int i, out PKEY k); void GetValue(ref PKEY k, out PV v); void SetValue(ref PKEY k, ref PV v); void Commit(); }
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown), ComImport]
public interface IMMDevice { void Activate(ref Guid iid, int ctx, IntPtr p, [MarshalAs(UnmanagedType.IUnknown)] out object o); void OpenPropertyStore(int access, out IPropertyStore ps); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown), ComImport]
public interface IMMDeviceEnumerator { void EnumAudioEndpoints(int flow, int mask, out IntPtr c); void GetDefaultAudioEndpoint(int flow, int role, out IntPtr d); void GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, out IMMDevice d); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] public class MMDeviceEnumerator {}
public static class Fx {
  public static void Set(string id, uint value) {
    var en = (IMMDeviceEnumerator)new MMDeviceEnumerator(); IMMDevice d; en.GetDevice(id, out d);
    IPropertyStore ps; d.OpenPropertyStore(2, out ps);                                        // STGM_READWRITE
    var k = new PKEY { fmtid = new Guid("1da5d803-d492-4edd-8c23-e0c0ffee7f0e"), pid = 5 };   // PKEY_AudioEndpoint_Disable_SysFx
    var v = new PV { vt = 19, u = value };                                                    // VT_UI4
    ps.SetValue(ref k, ref v); ps.Commit();
  }
}
'@
foreach ($id in IDS) {
  try { [Fx]::Set($id, VALUE); "ok`t$id" } catch { "FAILED`t$id`t$($_.Exception.Message)" }
}
"""


def set_enhancements(ids, on):
    """Switch Windows 'audio enhancements' on or off for the sound devices with these ids. Needs administrator
    rights. Returns the error lines, if any."""
    if not ids:
        return []
    script = FXSET_PS.replace("IDS", ",".join(f"'{i}'" for i in ids)).replace("VALUE", "0" if on else "1")
    return [line for line in ps(script).splitlines() if line.startswith("FAILED") or line.startswith("error")]


USB_PS = r"""
$ErrorActionPreference='SilentlyContinue'
$pm = Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable
$id = (Get-PnpDevice -PresentOnly | ? { $_.InstanceId -like 'USB\*' -and $_.FriendlyName -match 'PATTERN' } | select -First 1).InstanceId
while ($id -like 'USB\*') {
  $pd = (Get-PnpDeviceProperty -InstanceId $id -KeyName DEVPKEY_Device_PowerData).Data
  $state = if ($pd -is [byte[]] -and $pd.Length -ge 8) { 'D' + ([BitConverter]::ToUInt32($pd, 4) - 1) } else { '?' }
  $off = ($pm | ? { $_.InstanceName -like "$id*" } | select -First 1).Enable
  $reg = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Enum\$id\Device Parameters"
  "DEV`t$((Get-PnpDevice -InstanceId $id).FriendlyName)`t$state`t$off`t$($reg.SelectiveSuspendEnabled)`t$($reg.EnhancedPowerManagementEnabled)`t$id"
  $id = (Get-PnpDeviceProperty -InstanceId $id -KeyName DEVPKEY_Device_Parent).Data
}
"PLAN`t" + ((powercfg /query SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 | Select-String '0x[0-9a-fA-F]{8}\s*$' | % { $_.Line.Trim().Split(' ')[-1] }) -join ' ')
"AC`t" + (Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus | select -First 1).PowerOnline
"""
USB_STATE_PS = "$ErrorActionPreference='SilentlyContinue'; 'IDS' | % { $pd=(Get-PnpDeviceProperty -InstanceId $_ -KeyName DEVPKEY_Device_PowerData).Data; if ($pd -is [byte[]]) { 'D' + ([BitConverter]::ToUInt32($pd, 4) - 1) } else { 'gone' } }"


def usb_power(pattern):
    """(report lines, instance ids) for the adapter's USB device and every hub above it (Windows only).
    D0 = fully on; D2/D3 = suspended by Windows power saving."""
    lines, ids = [], []
    for row in (r.split("\t") for r in ps(USB_PS.replace("PATTERN", pattern.replace("'", "''"))).splitlines()):
        if row[0] == "DEV" and len(row) >= 7:
            name, state, off, sel, epm, dev_id = row[1:7]
            saving = [w for w, on in (("Windows may turn it off to save power", off == "True"),
                                      ("USB selective suspend enabled", sel == "1"),
                                      ("enhanced power management enabled", epm == "1")) if on]
            lines.append(f"{name or dev_id}: power state {state}; " + ("WARNING " + ", ".join(saving) if saving else "no power saving set"))
            ids.append(dev_id)
        elif row[0] == "PLAN" and len(row) > 1 and row[1]:
            vals = row[1].split()[-2:]     # last two numbers are the mains and battery settings
            names = [("on mains", vals[0])] + ([("on battery", vals[1])] if len(vals) > 1 else [])
            on = [w for w, v in names if int(v, 16)]
            lines.append("power plan: USB selective suspend is " + ("ENABLED " + " and ".join(on) + " (WARNING)" if on else "disabled"))
        elif row[0] == "AC" and len(row) > 1 and row[1]:
            lines.append("this PC is running " + ("on mains power" if row[1] == "True" else "ON BATTERY"))
    if not ids:
        lines.insert(0, f"no USB device found whose name matches '{pattern}'")
    return lines, ids


def monitor_usb(log, ids, stop):
    """Background: log when the adapter or a hub above it changes power state or disappears."""
    script = USB_STATE_PS.replace("'IDS'", ",".join("'" + i.replace("'", "''") + "'" for i in ids))
    last = None
    while not stop.wait(20):
        cur = ps(script).split()
        if last is not None and cur != last and len(cur) == len(ids):
            log("USB_POWER      CHANGE: " + "; ".join(f"{i.split(chr(92))[1]} {a} -> {b}" for i, a, b in zip(ids, last, cur) if a != b))
        if len(cur) == len(ids):
            last = cur


def sysinfo():
    if WINDOWS:
        return ps(SYSINFO_PS)
    try:
        return subprocess.run(["system_profiler", "SPHardwareDataType", "SPSoftwareDataType", "SPAudioDataType"],
                              capture_output=True, text=True, timeout=60).stdout
    except Exception as e:
        return f"{platform.platform()} ({e})"


def amd_state():
    """'enabled', 'disabled', or None when this PC has no AMD USB Audio Device."""
    if not WINDOWS:
        return None
    out = ps(f"$d = Get-PnpDevice | ? InstanceId -like '{AMD_ID}*' | select -First 1; if ($d) {{ [int]$d.ConfigManagerErrorCode }}")
    return {"22": "disabled", "": None}.get(out, "enabled" if out.isdigit() else None)


def set_amd(state):
    out = subprocess.run(["pnputil", f"/{state[:-1]}-device", "/deviceid", AMD_ID], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW).stdout
    return (out.strip().splitlines() or ["no output from pnputil"])[-1]


def is_admin():
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def run_elevated(args):
    """Start another copy of this program as administrator. False if the user says no."""
    import ctypes
    cmd = self_cmd() + args
    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"   # packaged exe: the new copy must unpack itself, it may outlive this one
    try:
        return ctypes.windll.shell32.ShellExecuteW(None, "runas", cmd[0], subprocess.list2cmdline(cmd[1:]), None, 1) > 32
    finally:
        del os.environ["PYINSTALLER_RESET_ENVIRONMENT"]


def register_resume(out, headless):
    exe, *pre = self_cmd()
    q = lambda s: s.replace("'", "''")
    arg = " ".join(f'"{a}"' for a in pre + ["--continue", str(out)]) + (" --headless" if headless else "")
    return ps(f"$a=New-ScheduledTaskAction -Execute '{q(exe)}' -Argument '{q(arg)}';"
              "$t=New-ScheduledTaskTrigger -AtLogOn;"
              "$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;"
              "$p=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -RunLevel Highest -LogonType Interactive;"
              f"Register-ScheduledTask -TaskName {TASK} -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null; 'ok'")


def restart_pc(seconds, why):
    subprocess.run(["shutdown", "/r", "/t", str(seconds), "/c", why], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)


# ---------- a run: link, one or two tests, results files ----------

def results_table(st):
    """What passed and what failed, one column for each test: (headings, rows). A row is (check, cells) and a cell
    is (text, kind) with kind ok, fail, warn or none (not checked)."""
    ok, none = ("OK", "ok"), ("-", "none")

    def count(n, text):
        return none if n is None else ok if not n else (f"FAIL: {text}", "fail")

    def heard(p):
        h = p.get("heard")
        if not h:
            return none
        if not h["arrived"]:
            return ("FAIL: nothing arrived", "fail")
        return ok if not h["dropouts"] and h["dropout_s"] < 1 else (f"FAIL: lost {h['dropouts']} time(s), {h['dropout_s']} s", "fail")

    checks = [
        ("The other PC's signal arrived here", lambda p: ok if p["ever_received"] else ("FAIL: nothing received", "fail")),
        ("... and stayed, with no dropouts", lambda p: count(p["dropouts"] if p["ever_received"] else None,
                                                           f"{p['dropouts']} dropout(s), {p['dropout_s']} s")),
        ("... with no damaged audio (tone only)", lambda p: count(p["glitch_blocks"] if p["ever_received"] and p.get("signal", "tone") == "tone"
                                                                else None, f"{p['glitch_blocks']} damaged block(s)")),
        ("This PC's playback arrived at the other PC", heard),
        ("This PC's sound device kept running", lambda p: count(p["stalls"], f"stalled {p['stalls']} time(s), {p['stall_s']} s")),
        ("No errors from the audio driver", lambda p: count(p["driver_errors_in"] + p["driver_errors_out"],
                                                           f"{p['driver_errors_in'] + p['driver_errors_out']} buffer error(s)")),
        ("The Dante link stayed up", lambda p: count(p.get("link_changes"), f"changed {p.get('link_changes')} time(s)")),
        ("USB power stayed on", lambda p: count(p.get("usb_changes"), f"changed {p.get('usb_changes')} time(s)")),
    ]
    rows = [(name, [fn(p) for p in st["phases"]]) for name, fn in checks]
    fx = st.get("effects") or []
    if fx:
        warn = [w for w, hit in (("enhancements enabled", any("NOT switched off" in e for e in fx)),
                                 ("third-party effects attached", any("THIRD-PARTY" in e for e in fx))) if hit]
        rows.append(("Audio processing on the Dante device", [(("WARNING: " + ", ".join(warn), "warn") if warn else ok)] * len(st["phases"])))
    return ["Check"] + [p["label"] + ("" if p.get("signal", "tone") == "tone" else f", {p['signal']}") for p in st["phases"]], rows


def summary(st):
    lines = [f"DanteTest {VERSION} summary - {HOST}", "",
             "This PC measures the audio it RECEIVES: the other PC plays the test signal, it travels over",
             "Dante, and this PC records it. Audio PLAYED by this PC is measured in the other PC's log.", ""]
    for n, p in enumerate(st["phases"], 1):
        lines += [f"Test {n}: {p['label']}", f"  {p['start']} to {p['end']}",
                  f"  RESULT: {verdict(p)}",
                  f"  listened {p['listened_s']} s, clean signal {p['tone_s']} s, dropouts {p['dropouts']} "
                  f"(total {p['dropout_s']} s, longest {p['longest_dropout_s']} s), damaged blocks {p['glitch_blocks']}",
                  f"  audio driver buffer errors: input {p['driver_errors_in']}, output {p['driver_errors_out']}; "
                  f"input stalls {p['stalls']} ({p['stall_s']} s)", ""]
    if st.get("exclusive"):
        lines += ["Tests ran in EXCLUSIVE mode: Windows' mixer and audio effects were bypassed.", ""]
    if st.get("effects"):
        lines += ["Audio processing found on the Dante device:"] + [f"  {e}" for e in st["effects"]] + [""]
    if st.get("usb"):
        lines += ["USB power (the adapter, then each hub above it):"] + [f"  {e}" for e in st["usb"]] + [""]
    if st["phases"]:
        heads, rows = results_table(st)
        wide = [max(len(heads[0]), *(len(r[0]) for r in rows))] + [max(len(h), *(len(r[1][n][0]) for r in rows)) for n, h in enumerate(heads[1:])]
        lines += ["What passed and what failed:", "  " + "  |  ".join(h.ljust(w) for h, w in zip(heads, wide))]
        lines += ["  " + "  |  ".join(t.ljust(w) for t, w in zip([name] + [c[0] for c in cells], wide)) for name, cells in rows] + [""]
    lines.append("Details with timestamps are in events.log; the PC and driver details are in sysinfo.txt.")
    return "\n".join(lines)


def save(st, out):
    (out / "state.json").write_text(json.dumps(st, indent=1))
    (out / "summary.txt").write_text(summary(st) + "\n")


def new_out_dir(base=None):
    stamp = f"{HOST}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    beside = Path(sys.executable).parent if FROZEN else Path.cwd()   # next to the exe; a Mac app uses the home folder instead
    bases = [Path(base)] if base else [beside / "DanteTest-results"] * (WINDOWS or not FROZEN) + [Path.home() / "DanteTest-results"]
    for b in bases:
        try:
            (b / stamp).mkdir(parents=True, exist_ok=True)
            return b / stamp
        except OSError:
            pass
    raise OSError("cannot create a results folder")


def new_state(device_in, device_out, minutes, adapters, link, matrix, headless=False, exclusive=False, usb_power=False, signal="tone",
              record=False, monitor=None):
    return {"phases": [], "device_in": device_in, "device_out": device_out, "minutes": minutes or None, "adapters": adapters,
            "link": link, "matrix": matrix, "headless": headless, "exclusive": exclusive, "usb_power": usb_power, "signal": signal, "record": record, "monitor": monitor,
            "step": "start", "effects": [], "usb": []}


def amd_label(state):
    return f"AMD USB Audio Device {state.upper()}" if state else "this PC as it stands"


class Session:
    """Runs in a worker thread. run() returns 'done', 'restart' (mid full test) or 'restart-final'."""

    def __init__(self, out, st, sink, on_status, gate=None, peer=lambda: None):
        self.out, self.st, self.on_status, self.gate, self.peer = out, st, on_status, gate, peer
        self.log = Log(out / "events.log", sink)
        self.stop = threading.Event()

    def report_effects(self):
        """Effects on the Dante device can mute or alter the test tone, so they go in the log and the summary."""
        lines = audio_effects(self.st["device_in"], self.st["device_out"])
        for line in lines:
            self.log(f"AUDIO_FX       {line}")
        if any("THIRD-PARTY" in line for line in lines) and not self.st.get("exclusive"):
            self.log("AUDIO_FX       WARNING: third-party audio processing is attached to the Dante device. "
                     "Repeat the test with 'Bypass Windows audio effects' ticked to see whether it is involved.")
        self.st["effects"] = sorted(set(self.st.get("effects", []) + lines))

    def report_usb(self):
        """Optional: USB power-saving settings for the adapter and its hubs. Returns the ids to watch."""
        if not (WINDOWS and self.st.get("usb_power")):
            return []
        lines, ids = usb_power(self.st["device_out"])
        for line in lines:
            self.log(f"USB_POWER      {line}")
        self.st["usb"] = lines
        return ids

    def phase(self, label):
        st = self.st
        pair = setup_link(self.log, st["adapters"]) if st["link"] else None
        link_stop = threading.Event()
        usb_ids = self.report_usb()
        if usb_ids:
            threading.Thread(target=monitor_usb, args=(self.log, usb_ids, link_stop), daemon=True).start()
        if pair:
            st["adapters"] = pair
            threading.Thread(target=monitor_link, args=(self.log, pair, link_stop), daemon=True).start()
        undo = monitor_patch(self.log, st.get("monitor"), pair) if st["link"] else None
        if undo and pair and (st.get("monitor") or "").endswith("=" + TURN):
            threading.Thread(target=monitor_follow, args=(self.log, st["monitor"], pair, link_stop), daemon=True).start()
        try:
            if self.gate:
                self.gate(self.log, self.stop)   # both PCs started together: wait until the other has done its checks too
            st["phases"].append(run_test(self.log, st, label, self.stop, self.on_status,
                                         self.out if st.get("record") else None, self.peer))
        finally:
            link_stop.set()
            if undo:
                monitor_patch(self.log, st.get("monitor"), pair, undo=undo)
        save(st, self.out)

    def run(self):
        st, log, out = self.st, self.log, self.out
        if st["step"] == "resume":
            ps(f"Unregister-ScheduledTask -TaskName {TASK} -Confirm:$false")
            state = amd_state()
            log(f"RESUMED        after restart; AMD USB Audio Device is now {state}")
            for _ in range(36):   # audio devices can take a while to appear after logon
                reinit_audio()
                if (pick_device(st["device_in"], "input") and pick_device(st["device_out"], "output")) or self.stop.wait(5):
                    break
            self.report_effects()
            self.phase(amd_label(state))
            log(f"RESTORE        {set_amd(st['original'])}")
            st["step"] = "finished"
            save(st, out)
            log("RESTART        restarting in 60 s to put the AMD USB Audio Device back as it was (to cancel: shutdown /a)")
            restart_pc(60, "DanteTest finished. Restarting to restore the AMD USB Audio Device setting.")
            return "restart-final"

        (out / "sysinfo.txt").write_text(f"DanteTest {VERSION} on {HOST}, {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n\n{sysinfo()}\n")
        state = amd_state()
        log(f"SYSTEM         DanteTest {VERSION}, {HOST}, {platform.platform()}, AMD USB Audio Device: {state or 'not present'}")
        self.report_effects()
        if st["matrix"] and not state:
            log("FULL_TEST      skipped: this PC has no AMD USB Audio Device; running a single test")
            st["matrix"] = False
        if not st["matrix"]:
            self.phase(amd_label(state))
            return "done"

        st["minutes"] = st["minutes"] or 5
        st["original"] = state
        self.phase(amd_label(state))
        if self.stop.is_set():
            log("FULL_TEST      stopped before the switch; the AMD USB Audio Device was not changed")
            return "done"
        log(f"SWITCH         {set_amd('disabled' if state == 'enabled' else 'enabled')}")
        st["step"] = "resume"
        save(st, out)
        log(f"RESUME_TASK    {register_resume(out, st['headless'])}")
        log("RESTART        restarting in 20 s. Log in afterwards and the test continues by itself.")
        restart_pc(20, "DanteTest: restarting to switch the AMD USB Audio Device. Log in and the test continues.")
        return "restart"


# ---------- finding the other PC's DanteTest ----------

PEER_PORT = 47847


class Peers:
    """Finds other DanteTest windows on the local network and passes Start and Stop between them.
    Plain UDP: a hello every second by broadcast and straight to each PC already found (which also keeps
    a firewall open for that PC's replies). Commands are sent three times; a repeat is ignored."""

    def __init__(self, on_command, state, settings=lambda: {}):
        self.on_command, self.state, self.settings = on_command, state, settings
        self.me = f"{HOST}-{os.getpid()}-{time.time():.0f}"
        self.found, self.done = {}, set()     # id -> (host, ip, state, last heard); ids of commands already handled
        self.cfg = {}                         # id -> the settings that PC last announced
        self.live = {}                        # id -> the counters of the test running (or last run) on that PC
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self.sock.bind(("", PEER_PORT))
        except OSError:
            self.sock = None                  # no network, or the port is taken: carry on without it
            return
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._hello, daemon=True).start()

    def _addresses(self):
        out = {"255.255.255.255"} | {ip for _, ip, _, _ in self.found.values()}
        try:
            import ifaddr
            import ipaddress
            for adapter in ifaddr.get_adapters():
                for ip in adapter.ips:
                    if isinstance(ip.ip, str) and not ip.ip.startswith("127.") and ip.network_prefix < 31:
                        out.add(str(ipaddress.ip_interface(f"{ip.ip}/{ip.network_prefix}").network.broadcast_address))
        except Exception:
            pass
        return out

    def _send(self, msg):
        data = json.dumps({"app": "DanteTest", "from": self.me, "host": HOST, **msg}).encode()
        for address in self._addresses():
            try:
                self.sock.sendto(data, (address, PEER_PORT))
            except OSError:
                pass

    def _hello(self):
        while True:
            try:
                self._send({"t": "hello", "state": self.state(), **self.settings()})
            except Exception:
                pass   # never let one bad hello stop the next: the other PC would think this one had gone
            time.sleep(1)

    def _listen(self):
        while True:
            try:
                data, (ip, _) = self.sock.recvfrom(4096)
                m = json.loads(data)
                if m.get("app") != "DanteTest" or m.get("from") == self.me:
                    continue
                self.found[m["from"]] = (m.get("host", ip), ip, m.get("state", "?"), time.time())
                if m.get("cfg"):
                    self.cfg[m["from"]] = m["cfg"]
                if m.get("t") == "hello":
                    self.live[m["from"]] = m.get("live")
                if m.get("t") in ("start", "stop") and m.get("id") not in self.done:
                    self.done.add(m.get("id"))
                    self.on_command(m)
            except (OSError, ValueError, KeyError, AttributeError):
                time.sleep(0.2)

    def others(self):
        """[(host, ip, state)] for the DanteTest windows heard from in the last 5 seconds."""
        now = time.time()
        return sorted((h, ip, st) for h, ip, st, t in self.found.values() if now - t < 5)

    def heard(self):
        """(host, counters) from the other PC's test: what it has received from this PC. None if no PC reports any."""
        now = time.time()
        return next(((v[0], self.live[k]) for k, v in sorted(self.found.items()) if now - v[3] < 5 and self.live.get(k)), None)

    def newest_settings(self):
        """The settings with the highest version among the PCs heard from lately, or None."""
        now = time.time()
        live = [self.cfg[k] for k, v in self.found.items() if now - v[3] < 5 and k in self.cfg]
        return max(live, key=lambda c: c.get("ver", 0), default=None)

    def command(self, what, **settings):
        if not self.sock:
            return
        msg = {"t": what, "id": f"{self.me}-{time.time()}", **settings}
        self.done.add(msg["id"])

        def send():
            for _ in range(3):
                self._send(msg)
                time.sleep(0.15)
        threading.Thread(target=send, daemon=True).start()


ICON_PNG = (   # the window's icon, 64 x 64; made by scripts/make-icon.py
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAREklEQVR42uWbeXBdV33HP+fce997erIky5b33bIs7za2bDMmRVDsACFMQqg6HV"
    "oYKJ0B2qTsJS3TKKaUpQyUBhqmtEA6TCeTekLixkkDMSVuNide4sj28yIv8hJ5VSzZ0lvuvef8+se971lP3iQTxza9njOWnt4957f/vr/fOQf+"
    "nz/qt3tdFK33q+vKwer7BZS8dQu2tmqaW11o1TeOHkXR3OrSOnSahqC9Vk1LRrFmjSl+Mn3lV2rEU2OMoUqseUstQWlHHCd5zmY5eWjD6u4yBa"
    "0GWG3fPAG0tDhFxqfc+tezQH0YWAV2NiIjldbOb+1NQ9c6Yq0FulBqt1L6Gavk0cNPfzMT0fyfDmv+0Pz2AoiZn/aee2eKo/9WkBbtJpJYg9gQ"
    "EQuCXKcIppTSKO2CdrBhIVDoR8WGXzu0/h92xbRbuDR9zmWnb23VPPignbLyK58RR6/RjrtUjO9a44cYIyCqnyCvxxBEEGOstb5VIp52E/OAjw"
    "+fvqK3+4l/3RjFqg1DtgBFS4tmzRozZeWXH9BexT0S5BCxISgHpVQkVHWjBMGIFhEBMSjtOl4FJsj9y6H13/n05Szh4hbQ3Ory1INm0u9/8QEn"
    "kb7H+NlAEK1QjoBS3JhPRJvSgog1Qegk0suqJzeN73ny3/6L5laXQxvslQXQ0uJEzH/+U04i/XfWzwaAdwOpe7DB3RETBDpRsax64pIzPRu+9R"
    "ItLQ6ZjFzaBVpbNatXy+T33DtNEW4XSCJG38BKH4xRWJQOtWVhx2++s5fWVsXq8ymyHDhkMgoQMYWvK8dLY0NBUIhwkw6FGNGOmzQSfBOQmMeL"
    "WUCrhtV2YvPnZ2hHZRBx4ih/s2q//2NRWkAtOPw/383Elm7LLaC59PMfKSfhiRgrgrp5ld9/iFVOwrHGfASAZ8/zfV4AG4j8QtlbxYYgxVT3Oz"
    "AEJTZEKVkV8Xq/GegCCpAZ77unupBV+5XWdSLmgoynlEJrhbWC1gqxghW5rratlUL1o8laieDAwGColBIrPdYz9Z2//ueuyL2VxBbQqgCyBT0O"
    "JSNETD+AEQ2tIAxDzvT0UvB9us/2kfd9tFaRnV0HzWqtyMe0FHyfMz29hGGIVgz8rkIsSlGjA8ZHLEdlvBv9AqwGFxkGSovYMpintaLgB9QMS9"
    "N6910snjOVvR3H+d7PnuLwsdOkU0miuuQt1LzWZLN5Jo+v40uf/AAzpozh1cwhvvuTJ+npzZJMuFhbZglWaUcrLVX9Ml4sgKK+rVGgY7wYyUAB"
    "obF4nsPD/3g3b5szFYCm+dN559JZvO/PvkXXmV48z4kM4ZrXRQqloOAHjK6r4fEHv8DEsSMimuZNZ8m8adz5me8SGotWqh8v0U9KpMyt9UVhtU"
    "jpf0cres728ZcffS9vmzOVQhBireAHIRPHjuD+uz9MNpdHK9XvvWs5BK0Ufbk8rXffxcSxI/D70bR4zlTu+eh76Tnbh6MH0iQQDrCkixcW0VAI"
    "BT9g/OjhfPyuZqwICddBKfDcyMTuWNnEvIZJsRC49n6vIJvLs2DmJO5c2YS1gue6MU0OVoQ//YNmxo+ppeAHqAvmuJwAAsoSqNaK3r4ct96ygL"
    "raKkQEpVQ8wIrguQ53rmoimytEArjGSV0ryOUK3LmyqcSwUpToEhFGDq/i1nfMp7cvdz5IF8dlBRBJoDQkFsLt715c9n7niTPR3+Is+f7mRVSl"
    "U4TGXnMLCI2lMp3ife9cVEqDIkLnyTOR/cZ03v7uxZfIUFdwAZHzebTgB0wYU8vbFzWgFDhxnr3vnx7hjZ7eCAuIMLt+Ao3Tx5PLF0pauBZDKU"
    "UuX2DW9PHMrp9QUtAbPb3c9/1HsDaKWUrB8oUNjB9dS94PyvgKCK4UA4oAIzK1t82ZRk1VmtAYlFIc6jzFo0+/zM72o5HNhAZHa1Ysnkm+4Mdx"
    "4FqBHsgXfFYsnonjaIIwwis724/yi1++zOHOUyilCI1heHWaxXOnkSu6JgzOAoo2FKU/wy1LGgEwJsrzL726l+6ubl7YsrskWYBbljTixOjwWv"
    "m/xBou0lRc+8UtuzlzupsXX91bRustSxojxfX3jcEKwBhLZSrBsoUzSsAD4LlXMrgJj5e27gGibACweO406mqrCMIwirxvMvMKIQhD6mqrWDx3"
    "WtnaL27dg5vweO6VTAm4ASxbOIN0yosEUpzrcmkwCoECCgq+z4SxI5hVPyFezKHgh2zdeYDq6jSZ9iMcO3WmhL/HjqqlYeo4cnkflEIG/COe94"
    "LPBvxerD4ueF8pcnmfhqnjGDuqtoT9j506Q6b9CNXVabbuPEDBD0uCmVU/gYljR1Lw/dLaV7CAsJRq8gWfeQ2TSKeShLGvtXd00nH0JJUVSU51"
    "9fDaro44DoQlK/D9IJq0THtRmrLGROAE0P2Cm46/Uwy8EuH28vQH+L5f0n5xzdd2dXCqq4fKiiQdR0/S3tEZcRIa0qkk8xomRbGJYkYIrxwEVT"
    "xB0/z6yKdinL9lx356+3K4jiYwIZteay97b+mCGWjdX9KRRoMgJJvL43kOPeeyYC19ojACRiCLwoQh+YLP+NG1iAiFgh81n0tgNtL4sgUzytbc"
    "9Fo7gQlxHU1vX46tOw6U0dw0v54wNJds6l0UCFlrSXoOi+dNL5XBAJtf24eKdmRIOA5btu8HwHWi3urCWVOpGZYmDMPzgTQw1Fan+fn3Pstzj3"
    "yDb37xI5wNDCuSeX455ji/HHOc5W6OwHF56Nt38/yab7D2x/cyflQtgR9E1YgIYRhSMyzNgllTy9bcsn0/CcdBrEUhJaUUaV48bzpJz42KtUEF"
    "QQW+HzBudC2zZ0xERHAdTWgMbbsPkky4GGNIJl32HDjKmZ5eHEcjIkyeUMfUiaMoFAK0AkdrzvZm+YuP3cZt71rC5PF1/PnHbuO231vIfW4nC1"
    "MBCysC7nM7uWtlE7evXEpFMsHSBQ186o/fy7m+LI5WaAWFQsDUCXVMnjAKEcFxNN1n+9hz4CjJZExTwqVtdwfGWBytsVaYM2MiY0bV4PvBRa3g"
    "olmgmF6qKiviJojm+Klu2juOkUx4GGvxXIfjp7rZte88HnAdh/mNU8gXokBorCGVdFm+sAFjLQU/wFphWDoVlarqfMvRWou1Ufax1rJkXj2pZA"
    "JjDShFvuAzf9bUyP3imJTZd4Tjp7rxXAdjLcmER3tHJ6+f6MJxNFpH8LjIvAwGCotYEgmX14938bUHHuFcX46uM+dY/f2H6cvmo1wvUamZL/hs"
    "2bG/LCc3LZgR/13w/ZCxo4bTWD8RR2sSnoe1ls2Zg3w1W0dbzqMt5/HVbB2bdh7EWovnuWitmVU/gbGjavD9EK0EEUtT7P8S1/lbtu+PwVdMk1"
    "Zk8wW+9Pc/o73jGAePnOALX/8pb3Sfw3V1tI85IAi6F7MAay2VFUl++O/rWLf+FYLQ0Hmii2HpFMaYEsOOVmxuay/DCYvnTiedSiBWyBcKzK6f"
    "TU1VOjJLR3Pw6AlOd57gsKRZdSwNgC+Q7jzBwaMnaJg6HmMsNVWVzK6fyDPHtpFKuKRTCRbPjWKSdqK1trS1xwqJMoU1hspUkl/976u8uGUXWi"
    "l6erORxRmD0mow5fB52FhVmaLzRBen3+hhWGWqzITEWlIJl517DlEoBHhuFJQap0e5NwhCTGhKWgtjwW3beYDus33UuAoHwUGocRXd5/rYljlY"
    "9t2mBTMwoSEIQiaOHUnj9POYJF8I2L7nEKmEW9aNEmsYVpnEGEMQhlQPoHvQSDCSqCXhuVHZ2R9NxVaSSLgc7jzJ3v65tyLJ/MYpZHN5EgmX5Y"
    "say7qvL2/bi4hg+s1lYpj78qt7ij0fAN6+qJFkwiWbyzO/cQrpivOYZF9HJ0c6T5JIuIi1ZVDXGhuXyJSjwMFkgYEIzIrFir3gc0FwtOZcb44t"
    "sRsUMfiKptlk8wXGjhrOwtkxbPWiBsrmtnYSsdZKa8TC3NzWHjU4vMiaFsyZxphRw8nmC6xYMrtsjc1t7ZzrzeFofVHaSlXkBYj0YgJYfR4IDq"
    "k4iVHjC5tjDO5EmrulaQ5aK+Y2TGZkbRXGWJRSHO48yb6Dr5NKuGVFk1ghlXDZd/D1UkVnjGXk8CrmNkyOCqClc8rWeGFLBl3aER/CuHw5HAyp"
    "OWGtIZX02LK9nXzBx3PdqD/QMInJ4+pYvmhmyacFeGXbHt7oPovrOHFELjZeLK7j8Eb3OV7ZtgeJ3Qlg+aKZTBpXx+yGyYhE7a98wWdLWzuppI"
    "e1ZohNlSvGgCFsuFghlfDoOHyc7bs7yvDAHavezoqmOaXqTAG/ebEt9nFhQLO21Lt79qXXUP00/Y6mOdyxanlZ/m/bdZCDh4+TSnixJV01/xem"
    "wf4doUE1qR1NX67A+udeZenCmSUI+uVPf5ia6spS2ZrNFXhhc4aKVAJj7QVrGGupSCV4ftNO+nIFKiuSUVqdP4P6qePK4O3657eRzRWoTKeQ0A"
    "5+p1wEM5Su8FDc4L+f3YQxFjfO0WNG1UZIzkTMvrB5JwcOdZJKeHHaGjiPJZXw2H/oGC9u2hllCmNJJROMqauN8b/GWMvTz266SvOXoaXBwQxr"
    "ItC0bcd+Nm7dVYrU/TWslOLhx39znvFLBihBjOXhtc+WtF20ymL037h1F9t27KeyInlBah5cEAzfXAso7h8Ya/jBz9ZGdb9YrAhBEOI4mj37j/"
    "DE+o1UD6uIGbn4PMZYqqsqWLd+I7v3HYn6fkGIlSgdK6X4wU/XYoy5SL//OgTB4jDGMHxYmnXPbOTnj67Hc10crfE8F98P+FzrjyjkfRylIDpX"
    "eIlhcZQmny/wudYf4fsBnhfP5br8/NH1rFu/keFV6QiSD3FH6YpBUCkjRvRV7e8ZsaTTST7b+iD7D3Xy/ncvo+vMWR74yWM898oOaqoqCa0pHe"
    "+71BPakGGVFTy/aTsf/MR9fPaTH2JkbTVP/fplfvjQWtIVSYzYfnt+Qz5HdrFDUtHxmJHzWxodJZnYMoZ8ELC4Pdhzro9UMkEQhmilqaqsKHVo"
    "Bvs4WnOuLxdhhBj711RVlta4un1VBZYFJ7ev2V7kueyARO2SlhrXt/uV1iMZsEU+JOKdqBlRDGRXu3VerDCLGyDFQHjVRwjFnnUKyfpjex8+Xe"
    "RZDYgHdvT8Dz2vtPcOsYEB5fA78YhR2nPEBptPbn9saX8/7HdIqjkWt3omPgn7Vux1v1VnhASlsLA+5tW5yCGpd1kAq+3DJgxCiTrX/E4MRFsT"
    "WEfxH/15HZAGV1taWpzTbY/vRcxjynE10XGxm1z7xijtacQ+eaLtsR20tDj9L1OU1wJr5gigxDpfxQQfBLwY0t3MR2UREwaO2HsBFfN4qcPSG4"
    "SWFie74dHTlXUNZ7Wb/IDYMETh3KTsh9pNusYU7j2164m10WHpB+3lT4tnMkJzs5vd/KuNFSPqx2kvuUxMGMS70+omUbsghNpLejbIP9S1a91X"
    "+l/7ufJ9gUOHYkt47ImKEfXjlJtYhhiFSBjFDfUWnAa7mhPyQnRhQmnlJhwJCw+d3rXuE9DikFljh3ZlJpMBWnX29I+fSI+Y0YOiWTluMsLyEu"
    "+Bo67/UfpiGSkGhVba00BBbPg3XbvW/VWksIxcSmNX8O0NRSG8lBzZsFajRgGN2nFdUDpq5AjXKcSXrk0p5SitXS3RDa7HlbJ/cjrz5C/i+0Jy"
    "OXNVQ702N7rxjvmiTYsgqxAagdrrZgSR8rtB71Hwa7Rdcyrz1LaBNL95Fycpv5A4bubtdVbLWD/IVjPgBOa1d3klCS99VoXmxPF9T5+6HJ1v8t"
    "OqaW52bzBsoCKarunV2Uu936ogc52EMUdgtdyAKenmef4PpIs21UHJNqEAAAAASUVORK5CYII=")


# ---------- window ----------

COLOURS = {"idle": "#6c757d", "waiting": "#c77700", "ok": "#1e8e3e", "dropout": "#c5221f", "stalled": "#c5221f"}
# Everything the window colours itself (the picture and the results table) comes from one of these two sets,
# so text is readable whether the computer is in light or dark mode.
LIGHT = {"canvas": "#ffffff", "box": "#eef1f4", "line": "#9aa0a6", "text": "#202124", "muted": "#5f6368", "plot": "#f6f7f8",
         "off": "#c0c4c8", "blue": "#1a73e8", "lost": "#f6c7c5", "wait": "#fbe3c0",
         "ok": "#d7f0dd", "fail": "#f6c7c5", "warn": "#fbe3c0", "none": "#eef1f4", "head": "#dfe3e7", "cell": "#ffffff"}
DARK = {"canvas": "#1f2023", "box": "#35373b", "line": "#6f747a", "text": "#e8eaed", "muted": "#a4a9ae", "plot": "#2a2b2f",
        "off": "#5f6368", "blue": "#8ab4f8", "lost": "#5a2320", "wait": "#4d3a14",
        "ok": "#1f4a2c", "fail": "#74261f", "warn": "#5c4313", "none": "#35373b", "head": "#44474c", "cell": "#2a2b2f"}


def palette(root):
    """DARK when a Mac is in dark mode, else LIGHT (Windows draws these windows light in both modes)."""
    try:   # the Mac's own window background colour: dark grey in dark mode, light grey in light mode
        dark = sys.platform == "darwin" and sum(root.winfo_rgb("systemWindowBackgroundColor")) < 3 * 32768
    except Exception:
        dark = False
    return DARK if dark else LIGHT


class App:
    """One window: settings in numbered steps down the left, start buttons, status and log on the right."""

    def __init__(self, root, args):
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
        self.messagebox, self.root, self.args = messagebox, root, args
        self.q, self.session, self.thread, self.out, self.amd, self.adapters = queue.Queue(), None, None, None, None, []
        root.title(f"DanteTest {VERSION} - {HOST}")
        try:
            self.icon = tk.PhotoImage(data=ICON_PNG)
            root.iconphoto(True, self.icon)
        except Exception:
            pass   # an icon is not worth failing to start for
        root.minsize(980, 560)
        root.protocol("WM_DELETE_WINDOW", self.close)
        pad = {"padx": 6, "pady": 2}
        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)
        ttk.Label(main, text="Open DanteTest on both computers, then press Start test on one of them: the other starts too. "
                  "Each one shows the audio it receives from the other.").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        left = ttk.Frame(main)
        left.grid(row=1, column=0, sticky="ns", padx=(0, 10))
        right = ttk.Frame(main)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(6, weight=1)

        def section(title):
            f = ttk.LabelFrame(left, text=title, padding=6)
            f.pack(fill="x", pady=(0, 6))
            f.columnconfigure(1, weight=1)
            return f

        # ----- left: the settings, in the order you need them
        audio = section("1   Sound device: the Dante adapter on this PC")
        self.cb_out, self.cb_in = self.combo(audio, 0, "Play to"), self.combo(audio, 1, "Listen on")
        for cb, kind in ((self.cb_out, "output"), (self.cb_in, "input")):
            cb["values"] = [d["name"] for _, d in devices(kind)]
            cb.set((pick_device(args.device, kind) or (0, {"name": ""}))[1]["name"])

        dante = section("2   Dante link")
        self.link_var = tk.BooleanVar(value=not args.no_link)
        ttk.Checkbutton(dante, text="Link the two adapters automatically, both directions",
                        variable=self.link_var).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        self.cb_a, self.cb_b = self.combo(dante, 1, "This PC's adapter"), self.combo(dante, 2, "Link it to")
        self.dante_var = tk.StringVar()
        ttk.Label(dante, textvariable=self.dante_var, wraplength=300, justify="left").grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        self.btn_find = ttk.Button(dante, text="Search again", command=self.find)
        self.btn_find.grid(row=3, column=1, sticky="e", **pad)
        hear = ttk.Frame(dante)
        hear.grid(row=4, column=0, columnspan=2, sticky="w")
        self.hear = tk.BooleanVar(value=False)
        ttk.Checkbutton(hear, text="Hear", variable=self.hear).pack(side="left", **pad)
        self.cb_src = ttk.Combobox(hear, state="readonly", width=19)
        self.cb_src.pack(side="left")
        ttk.Label(hear, text="on").pack(side="left", padx=4)
        self.cb_mon = ttk.Combobox(hear, state="readonly", width=22)
        self.cb_mon.pack(side="left")

        test = section("3   Test")
        row = ttk.Frame(test)
        row.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(row, text="Signal").pack(side="left", **pad)
        self.signal = ttk.Combobox(row, state="readonly", values=SIGNALS, width=8)
        self.signal.set(args.signal)
        self.signal.pack(side="left", **pad)
        ttk.Label(row, text="   Minutes (0 = until stopped)").pack(side="left", **pad)
        self.minutes = tk.StringVar(value=f"{args.minutes:g}" if args.minutes else "0")
        ttk.Spinbox(row, from_=0, to=1440, width=5, textvariable=self.minutes).pack(side="left", **pad)
        ttk.Label(test, text="The other PC is given the same signal and minutes when it is started from here.").grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        self.exclusive, self.usb_power, self.record = tk.BooleanVar(value=args.exclusive), tk.BooleanVar(value=True), tk.BooleanVar(value=args.record)
        boxes = [(self.record, "Save recordings as WAV files (about 6 MB a minute)")]
        if WINDOWS:
            boxes = [(self.exclusive, "Bypass Windows audio effects (exclusive mode)"),
                     (self.usb_power, "Check USB power saving on the adapter and its hubs")] + boxes
        for n, (var, text) in enumerate(boxes, 2):
            ttk.Checkbutton(test, text=text, variable=var).grid(row=n, column=0, columnspan=2, sticky="w", **pad)

        self.fx_state, self.fx_rows = [], []
        if WINDOWS:
            fx = section("4   Windows audio enhancements")
            self.fx_var = tk.StringVar(value="Checking...")
            ttk.Label(fx, textvariable=self.fx_var, wraplength=400, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
            self.fx_tree = ttk.Treeview(fx, show="tree", height=5, selectmode="none")
            self.fx_tree.column("#0", width=380)
            self.fx_tree.tag_configure("mine", font=("Segoe UI", 9, "bold"))
            self.fx_tree.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(6, 0), pady=2)
            bar = ttk.Scrollbar(fx, orient="vertical", command=self.fx_tree.yview)
            bar.grid(row=1, column=2, sticky="ns", pady=2)
            self.fx_tree.config(yscrollcommand=bar.set)
            self.fx_tree.bind("<ButtonRelease-1>", self.fx_click)
            ttk.Button(fx, text="Switch all off", command=lambda: self.apply_fx([r[3] for r in self.fx_state if r[2]], False)
                       ).grid(row=2, column=0, sticky="w", **pad)
            ttk.Button(fx, text="Refresh", command=self.refresh_fx).grid(row=2, column=1, sticky="w", **pad)
            for cb in (self.cb_out, self.cb_in):
                cb.bind("<<ComboboxSelected>>", lambda e: self.handle("fx", self.fx_state))
            self.refresh_fx()

        # ----- right: start, status, log
        buttons = ttk.Frame(right)
        buttons.grid(row=0, column=0, sticky="ew")
        big = ("Segoe UI", 11, "bold")   # the one button most people need, made obvious
        look = {} if sys.platform == "darwin" else dict(bg=COLOURS["ok"], fg="white", activebackground="#187a34", activeforeground="white",
                                                        disabledforeground="#cfe8d6", relief="flat")
        self.btn_mon = tk.Button(buttons, text="Start test", font=big, padx=18, pady=3, command=lambda: self.start(False), **look)
        self.btn_stop = tk.Button(buttons, text="Stop", font=big, relief="groove", padx=12, pady=3, command=self.stop, state="disabled")
        self.btn_full = ttk.Button(buttons, text="Full test: switch the AMD device and restart this PC",
                                   command=lambda: self.start(True), state="disabled")
        self.btn_mon.pack(side="left", padx=(0, 6))
        self.btn_stop.pack(side="left", padx=(0, 6))
        self.amd_var = tk.StringVar()
        ttk.Label(buttons, textvariable=self.amd_var).pack(side="right")

        peer = ttk.Frame(right)
        peer.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.remote = tk.BooleanVar(value=True)
        ttk.Checkbutton(peer, text="Work with the other PC: same settings, Start and Stop", variable=self.remote).pack(side="left")
        self.peer_var = tk.StringVar(value="Looking for the other PC...")
        ttk.Label(peer, textvariable=self.peer_var).pack(side="left", padx=10)
        self.stage, self.together = "ready", False   # stage: ready, checking, armed or testing; together: started with the other PC
        self.cfg, self.cfg_ver, self.mon_now = None, 0, ""   # the settings both PCs share, and their version
        self.sharing = True   # a copy of the tick box: the network threads must not touch the window's own variables
        self.live = None      # this PC's counters for the test running or last run, sent to the other PC
        self.net = Peers(lambda m: self.q.put(("peer", m)), lambda: self.stage if self.running() else "ready",
                         lambda: {"cfg": {**self.cfg, "ver": self.cfg_ver} if self.cfg and self.sharing else None, "live": self.live})

        self.status = tk.Label(right, text="Ready - press Start test", bg=COLOURS["idle"], fg="white",
                               font=("Segoe UI", 16, "bold"), pady=10)
        self.status.grid(row=2, column=0, sticky="ew", pady=(6, 2))
        self.counters = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.counters, justify="left").grid(row=3, column=0, sticky="w", pady=2)
        self.table = tk.Frame(right)   # what passed and failed; shown when a test finishes
        self.graph = tk.Canvas(right, height=184, highlightthickness=1)
        self.graph.grid(row=5, column=0, sticky="ew", pady=2)
        self.graph.bind("<Configure>", lambda e: self.draw())
        self.hist, self.sig_name, self.last = [], "", None
        self.text = scrolledtext.ScrolledText(right, height=8, width=64, state="disabled", wrap="none",
                                              font=("Consolas" if WINDOWS else "Menlo", 9 if WINDOWS else 11))
        self.text.grid(row=6, column=0, sticky="nsew", pady=2)
        bottom = ttk.Frame(right)
        bottom.grid(row=7, column=0, sticky="ew", pady=(4, 0))
        self.btn_open = ttk.Button(bottom, text="Open results folder", command=self.open_results, state="disabled")
        self.btn_open.pack(side="left")
        self.out_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.out_var).pack(side="left", padx=8)

        self.background(lambda: ("amd", amd_state()))
        if args.cont:   # carrying on a saved run: started elevated, or after the restart
            self.out = Path(args.cont)
            st = json.loads((self.out / "state.json").read_text())
            self.minutes.set(f"{st.get('minutes') or 0:g}")   # show the settings this run was started with
            self.signal.set(st.get("signal") or "tone")
            for cb, kind in ((self.cb_out, "output"), (self.cb_in, "input")):
                cb.set((pick_device(st[f"device_{kind[:-3]}"], kind) or (0, {"name": ""}))[1]["name"])
            for var, key in ((self.link_var, "link"), (self.exclusive, "exclusive"), (self.usb_power, "usb_power"), (self.record, "record")):
                var.set(bool(st.get(key)))
            self.launch(st)
        else:
            self.find()
        root.after(100, self.poll)

    def combo(self, parent, row, label):
        from tkinter import ttk
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
        cb = ttk.Combobox(parent, state="readonly", width=38)
        cb.grid(row=row, column=1, sticky="ew", padx=6, pady=3)
        return cb

    def background(self, fn):
        threading.Thread(target=lambda: self.q.put(fn()), daemon=True).start()

    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def refresh_fx(self):
        self.background(lambda: ("fx", enhancement_state()))

    def fx_click(self, event):
        """A device in the enhancements list was clicked: switch it the other way."""
        row = self.fx_tree.identify_row(event.y)
        if row:
            _, _, on, dev = self.fx_rows[int(row)]
            self.apply_fx([dev], not on)

    def apply_fx(self, ids, on):
        """Change the Windows setting for these devices, then show what Windows now says."""
        if self.running():
            self.messagebox.showinfo("DanteTest", "Stop the test before changing audio enhancements.")
        elif ids and is_admin():
            self.fx_var.set("Changing the setting...")
            self.background(lambda: (set_enhancements(ids, on), ("fx", enhancement_state()))[1])
        elif ids:
            self.fx_var.set("Windows is asking for administrator rights. The list updates in a few seconds...")
            run_elevated(["--headless", "--enhancements", "on" if on else "off", "--fx-ids", ",".join(ids)])
            for ms in (5000, 10000, 16000):   # the elevated copy takes a few seconds; then read the setting back
                self.root.after(ms, self.refresh_fx)

    def find(self):
        self.btn_find["state"] = "disabled"
        self.dante_var.set("Searching the network for Dante adapters (about 15 seconds)...")
        def look():
            devs = dante_devices()
            return "adapters", (devs, monitor_inputs(devs, na_json("subscription", "list"), usb_pair(devs)))
        self.background(look)

    def start(self, matrix, from_peer=False):
        if not self.cb_out.get() or not self.cb_in.get():
            return self.messagebox.showerror("DanteTest", "Choose the audio device to play to and to listen on.")
        try:
            minutes = float(self.minutes.get() or 0)
        except ValueError:
            return self.messagebox.showerror("DanteTest", "Minutes must be a number.")
        a, b = self.cb_a.get(), self.cb_b.get()
        if self.link_var.get() and len(self.adapters) > 2 and (not a or not b or a == b):
            return self.messagebox.showerror("DanteTest", f"{len(self.adapters)} Dante devices are on this network.\n\n"
                                             "Choose which one is plugged into this PC and which one to link it to.")
        if matrix and not self.messagebox.askokcancel(
                "Full test", f"This PC will restart twice.\n\n1. Test for {minutes or 5:g} minutes as it is now\n"
                "2. Switch the AMD USB Audio Device and restart\n3. After you log in, test again automatically\n"
                "4. Put the setting back and restart\n\nSave your work in other programs first. Continue?"):
            return
        # the monitor is driven by the PC where Start was pressed; the other PC shows the same setting but leaves it alone
        monitor = (f"{self.cb_mon.get()}={self.cb_src.get()}"
                   if self.hear.get() and self.cb_mon.get() and self.cb_src.get() and not from_peer else None)
        st = new_state(pattern_for(self.cb_in.get()), pattern_for(self.cb_out.get()), minutes,
                       [a, b] if a and b and a != b else None, self.link_var.get(), matrix, exclusive=self.exclusive.get(),
                       usb_power=self.usb_power.get(), signal=self.signal.get(),
                       record=self.record.get(), monitor=monitor)
        self.out = new_out_dir(self.args.out)
        save(st, self.out)
        self.together = from_peer or bool(self.remote.get() and self.net.others())
        if self.remote.get() and not from_peer:   # the other PC runs a plain test; during a full test it runs until stopped
            self.net.command("start", signal=st["signal"], minutes=0 if matrix else minutes)
        if matrix and WINDOWS and not is_admin():   # switching the device needs administrator rights
            if run_elevated(["--continue", str(self.out)]):
                self.root.destroy()
            else:
                self.messagebox.showerror("DanteTest", "Administrator rights are needed to switch the AMD USB Audio Device.")
            return
        self.launch(st)

    def launch(self, st):
        self.out_var.set(str(self.out))
        self.stage = "checking"
        self.session = Session(self.out, st, lambda line: self.q.put(("log", line)), lambda s: self.q.put(("status", s)),
                               self.wait_for_other_pc if self.together else None, self.net.heard)
        self.live, self.run_id = None, time.time()
        self.table.grid_remove()
        self.graph.grid()
        self.together = False   # only the run just started; a test that carries on after a restart starts by itself

        def work():
            try:
                self.q.put(("done", self.session.run()))
            except Exception as e:
                self.session.log(f"ERROR          {type(e).__name__}: {e}")
                self.q.put(("done", "done"))
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        for b in (self.btn_mon, self.btn_full, self.btn_find):
            b["state"] = "disabled"
        if sys.platform != "darwin":
            self.btn_mon["bg"] = COLOURS["idle"]   # grey while a test runs, green when it can be pressed
        self.btn_stop["state"] = self.btn_open["state"] = "normal"
        self.hist, self.last, self.sig_name = [], None, st.get("signal") or "tone"
        self.set_status("waiting", "Starting...")
        self.draw()

    def wait_for_other_pc(self, log, stop):
        """Runs in the test thread, after this PC's checks. The Dante link check takes a different time on each PC,
        so the test itself starts only when the other PC is ready too (or plainly is not going to be)."""
        self.stage, armed = "armed", time.time()
        log("SYNC           this PC is ready; waiting for the other PC to finish its checks")
        while not stop.is_set():
            stages = [st for _, _, st in self.net.others()]
            waited = time.time() - armed
            if any(st in ("armed", "testing") for st in stages):
                log("SYNC           both PCs are ready; starting together")
                break
            if waited > 90 or (waited > 10 and "checking" not in stages):
                log("SYNC           the other PC has not started a test; starting on this PC alone")
                break
            time.sleep(0.2)
        self.stage = "testing"

    def stop(self, from_peer=False):
        if self.session:
            self.session.stop.set()
            self.btn_stop["state"] = "disabled"
            if self.remote.get() and not from_peer:
                self.net.command("stop")

    def close(self):
        if self.running():
            self.session.stop.set()
            self.thread.join(5)
        self.root.destroy()

    def open_results(self):
        os.startfile(self.out) if WINDOWS else subprocess.run(["open", str(self.out)])

    def draw(self):
        """The picture: this PC and the other one with an arrow each way, and the level received over time."""
        c, s, pal = self.graph, self.last, palette(self.root)
        w, h = max(c.winfo_width(), 300), max(c.winfo_height(), 120)
        c.delete("all")
        c.config(bg=pal["canvas"], highlightbackground=pal["line"])
        state = s["state"] if s else "idle"
        colour = COLOURS[state]
        for x, name in ((8, "This PC"), (w - 118, getattr(self, "peer_host", None) or "Other PC")):
            c.create_rectangle(x, 8, x + 110, 58, fill=pal["box"], outline=pal["line"])
            c.create_text(x + 55, 33, text=name[:14], font=("Segoe UI", 10, "bold"), fill=pal["text"])
        out = pal["blue"] if self.running() else pal["off"]
        colour = pal["muted"] if state == "idle" else colour
        c.create_line(124, 22, w - 124, 22, fill=out, width=3, arrow="last")
        c.create_text(w / 2, 12, fill=out, text=f"this PC is playing {self.sig_name}: the other PC's window shows whether it arrives"
                      if self.running() else "not playing")
        c.create_line(w - 124, 44, 124, 44, fill=colour, width=5 if s else 3, arrow="last")
        c.create_text(w / 2, 56, fill=colour, font=("Segoe UI", 9, "bold"), text={
            "idle": "not listening", "waiting": "nothing received from the other PC yet", "stalled": "this PC's audio device has stopped",
            "ok": f"receiving from the other PC   {s['level']:.1f} dBFS" if s else "",
            "dropout": "NOTHING ARRIVING from the other PC"}[state])
        # level received over time, newest on the right, 2 pixels for each half second
        top, base, left = 76, h - 22, 44
        c.create_rectangle(left, top, w - 8, base, fill=pal["plot"], outline=pal["line"])
        c.create_text(left - 4, top + 6, anchor="e", text="0 dB", fill=pal["muted"])
        c.create_text(left - 4, base - 6, anchor="e", text="-80 dB", fill=pal["muted"])
        shown = self.hist[-((w - 8 - left) // 2):]
        for n, (st, level) in enumerate(shown):
            x = w - 8 - 2 * (len(shown) - n)
            if st != "ok":
                c.create_rectangle(x, top + 1, x + 2, base, width=0, fill=pal["lost"] if st in ("dropout", "stalled") else pal["wait"])
            y = base - (base - top) * min(1, max(0, (level + 80) / 80))
            c.create_rectangle(x, y, x + 2, base, width=0, fill=COLOURS[st])
        c.create_text(left + 2, h - 11, anchor="w", fill=pal["muted"],
                      text=f"level received, last {len(shown) // 2} s   (green = arriving, red = lost, amber = not started)")
        c.create_text(w - 8, h - 11, anchor="e", text="now", fill=pal["muted"])
        if self.mon_now and self.running():
            c.create_text(w - 12, top + 9, anchor="e", fill=pal["blue"], font=("Segoe UI", 9, "bold"), text=f"monitor: hearing {self.mon_now}")

    def show_table(self, st):
        """The table of what passed and failed, between the status bar and the picture."""
        import tkinter as tk
        for w in self.table.winfo_children():
            w.destroy()
        if not st["phases"]:
            return
        heads, rows = results_table(st)
        pal = palette(self.root)   # every cell sets both its background and its text colour
        self.table.config(bg=pal["line"])
        for col, text in enumerate(heads):
            tk.Label(self.table, text=text, font=("Segoe UI", 9, "bold"), bg=pal["head"], fg=pal["text"], anchor="w", padx=6, wraplength=230,
                     justify="left").grid(row=0, column=col, sticky="nsew", padx=1, pady=1)
        for r, (name, cells) in enumerate(rows, 1):
            tk.Label(self.table, text=name, bg=pal["cell"], fg=pal["text"], anchor="w", padx=6).grid(row=r, column=0, sticky="nsew", padx=1, pady=(0, 1))
            for col, (text, kind) in enumerate(cells, 1):
                tk.Label(self.table, text=text, bg=pal[kind], fg=pal["text"], anchor="w", padx=6,
                         font=("Segoe UI", 9, "bold" if kind == "fail" else "normal")).grid(row=r, column=col, sticky="nsew", padx=(0, 1), pady=(0, 1))
        for col in range(1, len(heads)):
            self.table.columnconfigure(col, weight=1)
        self.table.grid(row=4, column=0, sticky="ew", pady=2)
        self.graph.grid_remove()   # the table takes the picture's place until the next test, so the window stays one screen

    def set_status(self, colour, text):
        self.status.config(text=text, bg=COLOURS[colour])

    def write(self, line):
        self.text.config(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.config(state="disabled")

    def handle(self, kind, val):
        if kind == "log":
            self.write(val)
            if "MONITOR" in val:
                self.mon_now = val.split("now hearing ")[1] if "now hearing " in val else ""
                self.draw()
        elif kind == "amd":
            self.amd = val
            if WINDOWS and val:   # the full test only exists on a PC with the AMD device, so only show it there
                self.amd_var.set(f"AMD USB Audio Device: {val}")
                self.btn_full.pack(side="left")
                if not self.running():
                    self.btn_full["state"] = "normal"
        elif kind == "fx":
            self.fx_state = val
            if not WINDOWS:
                return
            mine = {r[3] for flow, cb in (("Render", self.cb_out), ("Capture", self.cb_in)) for r in val
                    if r[0] == flow and cb.get() and re.search(pattern_for(cb.get()), r[1], re.I)}
            self.fx_rows = sorted(val, key=lambda r: (r[3] not in mine, r[0] != "Render", r[1]))
            self.fx_tree.delete(*self.fx_tree.get_children())
            for n, (flow, name, on, dev) in enumerate(self.fx_rows):
                self.fx_tree.insert("", "end", iid=str(n), tags=("mine",) if dev in mine else (),
                                    text=f"{chr(0x2611) if on else chr(0x2610)}  {'Playback' if flow == 'Render' else 'Recording'}:  {name}")
            self.fx_tree["height"] = max(3, min(len(val), 8 if self.root.winfo_screenheight() >= 900 else 5))
            self.fx_var.set(f"Ticked = enabled ({sum(1 for r in val if r[2])} of {len(val)} sound devices). Click a device to switch it "
                            "off or on. Windows keeps the setting after a restart.")
        elif kind == "peer":   # Start or Stop pressed in the other PC's window
            who = val.get("host", "the other PC")
            if not self.remote.get():
                self.write(f"{who} asked this PC to {val['t']}; ignored because the box below Start is not ticked")
            elif val["t"] == "start" and not self.running():
                self.signal.set(val.get("signal") if val.get("signal") in SIGNALS else "tone")
                self.minutes.set(f"{float(val.get('minutes') or 0):g}")
                self.write(f"Started from {who}")
                self.start(False, from_peer=True)
            elif val["t"] == "stop" and self.running():
                self.write(f"Stopped from {who}")
                self.stop(from_peer=True)
        elif kind == "adapters":
            devs, inputs = val
            self.adapters = val = [d["name"] for d in devs]
            pair = usb_pair(devs)
            self.cb_a["values"] = self.cb_b["values"] = val
            if pair:
                self.cb_a.set(pair[0])
                self.cb_b.set(pair[1])
            self.cb_src["values"], self.cb_mon["values"] = [TURN] + (pair or val), inputs   # hear on a free monitor input
            self.cb_src.set(self.cb_src.get() or TURN)
            self.cb_mon.set(self.cb_mon.get() if self.cb_mon.get() in inputs else (inputs or [""])[0])
            self.dante_var.set("Two adapters found; they will be linked to each other." if pair else
                               {0: "No Dante adapters found. Is this PC on the same network as the adapters?",
                                1: "Only one Dante adapter found; the test needs two."}.get(
                len(val), f"{len(val)} Dante devices found. Choose which adapter is plugged into this PC and which one to link it to."))
            if not self.running():
                self.btn_find["state"] = "normal"
        elif kind == "status":
            head = {"waiting": "Waiting for the other PC's signal", "ok": f"RECEIVING OK   {val['level']:.1f} dBFS",
                    "dropout": "DROPOUT - signal lost", "stalled": "AUDIO DEVICE STALLED"}[val["state"]]
            self.set_status(val["state"], head)
            self.live = {"id": self.run_id, "running": True, "arrived": bool((self.live or {}).get("arrived")) or val["state"] == "ok",
                         "dropouts": val["dropouts"], "dropout_s": round(val["dropout_s"], 1)}
            self.last = val
            self.hist = (self.hist + [(val["state"], val.get("now", val["level"]))])[-2000:]
            self.draw()
            left = f", {clock(val['left'])} left" if val["left"] is not None else ""
            self.counters.set(f"{val['label']}   |   running {clock(val['elapsed'])}{left}\n"
                              f"dropouts {val['dropouts']} ({val['dropout_s']:.1f} s)   |   damaged blocks {val['glitches']}   |   "
                              f"driver errors {val['driver_errors']}")
        elif kind == "done":
            self.btn_stop["state"] = "disabled"
            self.live = {**self.live, "running": False} if self.live else None
            self.show_table(self.session.st)
            self.last = None
            self.root.after(300, self.draw)   # once the worker has finished: arrows back to idle, the history stays
            if val == "restart":
                return self.set_status("waiting", "Restarting this PC in 20 s - log in and the test continues")
            self.write("\n" + summary(self.session.st))
            if val == "restart-final":
                return self.set_status("idle", "Finished - restarting in 60 s to put the AMD setting back")
            phases = self.session.st["phases"]
            ok = bool(phases) and not any(kind == "fail" for _, cells in results_table(self.session.st)[1] for _, kind in cells)
            self.set_status("ok" if ok else "dropout", "Finished - PASS" if ok else "Finished - FAIL (see below)")
            self.btn_mon["state"] = self.btn_find["state"] = "normal"
            if sys.platform != "darwin":
                self.btn_mon["bg"] = COLOURS["ok"]
            self.btn_full["state"] = "normal" if self.amd else "disabled"

    def shared(self):
        """The settings both PCs should agree on. The two adapter boxes are mirrored on the other PC."""
        return {"signal": self.signal.get(), "minutes": self.minutes.get(), "a": self.cb_a.get(), "b": self.cb_b.get(),
                "hear": bool(self.hear.get()), "src": self.cb_src.get(), "mon": self.cb_mon.get()}

    def share_settings(self):
        """A change made here gets a higher version number and the other PC copies it; and the other way round."""
        now, self.sharing = self.shared(), bool(self.remote.get())
        if now != self.cfg:
            self.cfg, self.cfg_ver = now, self.cfg_ver + 1
        theirs = self.net.newest_settings()
        if not (theirs and self.remote.get() and theirs.get("ver", 0) > self.cfg_ver and not self.running()):
            return
        if theirs.get("signal") in SIGNALS:
            self.signal.set(theirs["signal"])
        self.minutes.set(str(theirs.get("minutes", "0")))
        for cb, value in ((self.cb_a, theirs.get("b")), (self.cb_b, theirs.get("a")),
                          (self.cb_src, theirs.get("src")), (self.cb_mon, theirs.get("mon"))):
            if value in cb["values"]:
                cb.set(value)
        self.hear.set(bool(theirs.get("hear")))
        self.cfg, self.cfg_ver = self.shared(), theirs["ver"]

    def poll(self):
        self.share_settings()
        others = self.net.others()
        self.peer_host = others[0][0] if others else None
        self.peer_var.set("   ".join(f"Found {h} ({ip}): {st}" for h, ip, st in others) or "Other PC not found yet: open DanteTest on it")
        try:
            while True:
                self.handle(*self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(150, self.poll)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--headless", action="store_true", help="no window: run one test from the options below and exit")
    ap.add_argument("--minutes", type=float, help="length of each test (default: until stopped; 5 for the full test)")
    ap.add_argument("--device", default="Dante|USB Audio Device",
                    help="text that identifies the Dante adapter's audio device (default: a device with Dante in its name, "
                         "or else one called USB Audio Device, which is how some Dante adapters appear)")
    ap.add_argument("--adapters", help="names of the two Dante adapters to link, A,B (default: auto when exactly two are found)")
    ap.add_argument("--no-link", action="store_true", help="do not touch the Dante routing")
    ap.add_argument("--monitor", metavar="'DEVICE input N=ADAPTER'",
                    help="also patch ADAPTER's channel 1 to input N of another Dante device (a monitor speaker) for the test, "
                         "so it can be heard; only if that input is free, and taken off again afterwards")
    ap.add_argument("--signal", choices=SIGNALS, default="tone",
                    help="test signal; use the same on both PCs. speech uses the PC's built-in voice (default: tone)")
    ap.add_argument("--record", action="store_true",
                    help="save the signal played and all the audio received as WAV files in the results folder")
    ap.add_argument("--exclusive", action="store_true",
                    help="Windows: open the device in exclusive mode, bypassing Windows' mixer and audio effects")
    ap.add_argument("--usb-power", action="store_true",
                    help="Windows: report USB power-saving settings for the adapter and watch its power state during the test")
    ap.add_argument("--enhancements", choices=("on", "off", "show"),
                    help="Windows: show, or switch on or off, audio enhancements for the sound devices matching --device, then "
                         "exit. On and off need admin; Windows keeps the setting after a restart. --device . means every device")
    ap.add_argument("--fx-ids", help=argparse.SUPPRESS)                               # internal: exact devices to change
    ap.add_argument("--matrix", action="store_true",
                    help="full test: test, switch the AMD USB Audio Device, restart, test again, restore (needs admin)")
    ap.add_argument("--out", help="results folder (default: DanteTest-results next to the program)")
    ap.add_argument("--continue", dest="cont", help=argparse.SUPPRESS)   # internal: carry on a run saved in this folder
    args = ap.parse_args()

    say = (lambda line: print(line, flush=True)) if sys.stdout else (lambda line: None)
    if args.enhancements:
        if not WINDOWS:
            sys.exit("--enhancements is for Windows.")
        ids = args.fx_ids.split(",") if args.fx_ids else [r[3] for r in enhancement_state() if re.search(args.device, r[1], re.I)]
        if args.enhancements != "show":
            if not is_admin():
                sys.exit("Switching audio enhancements needs an administrator command prompt.")
            for line in set_enhancements(ids, args.enhancements == "on"):
                say(line)
        for flow, name, on, dev in sorted(enhancement_state()):
            if dev in ids:
                say(f"{'Playback ' if flow == 'Render' else 'Recording'}  enhancements {'ENABLED ' if on else 'off     '}  {name}")
        return

    if not args.headless:
        try:
            import tkinter as tk
        except ImportError:
            sys.exit("This copy of Python has no window support (tkinter). Add --headless to run without the window.")
        root = tk.Tk()
        App(root, args)
        return root.mainloop()

    if args.cont:
        out = Path(args.cont)
        st = json.loads((out / "state.json").read_text())
    else:
        if args.matrix and WINDOWS and not is_admin():
            sys.exit("The full test needs administrator rights.")
        out = new_out_dir(args.out)
        st = new_state(args.device, args.device, args.minutes, args.adapters.split(",") if args.adapters else None,
                       not args.no_link, args.matrix, headless=True, exclusive=args.exclusive, usb_power=args.usb_power,
                       signal=args.signal, record=args.record, monitor=args.monitor)
    say(f"DanteTest {VERSION} - results folder: {out}")
    session = Session(out, st, say, lambda s: None)
    worker = threading.Thread(target=session.run, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)
    except KeyboardInterrupt:
        session.stop.set()
        worker.join(10)
    say("\n" + summary(st))


if __name__ == "__main__":
    main()
