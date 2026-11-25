"""Draws the DanteTest icon (a sine wave with a gap: a dropout) and writes assets/icon.png, DanteTest.ico and
DanteTest.icns, plus the small copy embedded in src/dantetest.py for the window's own icon.
Run on a Mac from the project folder: .venv/bin/python scripts/make-icon.py   (needs: pip install pillow)"""
import base64, io, math, re, subprocess, tempfile
from pathlib import Path
from PIL import Image, ImageDraw

S = 2048                                    # drawn large, then scaled down so the edges are smooth
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
m, r = int(S * 0.06), int(S * 0.22)
for y in range(m, S - m):                   # dark blue background, lighter at the top
    t = (y - m) / (S - 2 * m)
    d.line([(m, y), (S - m, y)], fill=(int(20 + 12 * (1 - t)), int(40 + 30 * (1 - t)), int(74 + 46 * (1 - t)), 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([m, m, S - m, S - m], radius=r, fill=255)
img.putalpha(mask)

def stroke(points, colour, width):       # a line as closely spaced dots: smooth, with round ends
    for x, y in points:
        d.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=colour)

left, span, thick = S * 0.14, S * 0.72, S * 0.062
y_at = lambda x: S / 2 - S * 0.2 * math.sin((x - left) / span * 2.5 * 2 * math.pi)
gap = (left + span * 2 / 5, left + span * 3 / 5)          # the middle half-wave is missing: a dropout
white, red = (255, 255, 255, 255), (255, 82, 70, 255)
stroke([(x, y_at(x)) for x in range(int(left), int(gap[0]) + 1, 2)], white, thick)
stroke([(x, y_at(x)) for x in range(int(gap[1]), int(left + span) + 1, 2)], white, thick)
stroke([((gap[0] + gap[1]) / 2, S / 2)], red, thick * 0.95)   # a red dot where the audio should be

assets = Path("assets")
big = img.resize((1024, 1024), Image.LANCZOS)
big.save(assets / "icon.png")
big.save(assets / "DanteTest.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
with tempfile.TemporaryDirectory() as tmp:  # the Mac icon file is made by Apple's own tool
    iconset = Path(tmp) / "DanteTest.iconset"
    iconset.mkdir()
    for n in (16, 32, 128, 256, 512):
        big.resize((n, n), Image.LANCZOS).save(iconset / f"icon_{n}x{n}.png")
        big.resize((2 * n, 2 * n), Image.LANCZOS).save(iconset / f"icon_{n}x{n}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(assets / "DanteTest.icns")], check=True)

buf = io.BytesIO()
big.resize((64, 64), Image.LANCZOS).save(buf, "PNG", optimize=True)
b64 = base64.b64encode(buf.getvalue()).decode()
lines = "\n".join(f'    "{b64[i:i + 110]}"' for i in range(0, len(b64), 110))
src = Path("src/dantetest.py")
text = src.read_text()
new = f"ICON_PNG = (   # the window's icon, 64 x 64; made by scripts/make-icon.py\n{lines})\n"
text, n = re.subn(r"ICON_PNG = \(.*?\)\n", lambda _: new, text, flags=re.S)
if not n:
    text = text.replace("\n# ---------- window ----------\n", f"\n{new}\n\n# ---------- window ----------\n", 1)
src.write_text(text)
print("icon written:", sorted(p.name for p in assets.iterdir()), "| embedded", len(b64), "characters")
