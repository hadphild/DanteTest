#!/bin/bash
# Builds dist/DanteTest.dmg on a Mac: a DanteTest.app that needs nothing installed. Run from the project folder.
# The app is for the kind of Mac it is built on (Apple silicon or Intel) and is not signed with an Apple Developer ID.
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/pip install -q --disable-pip-version-check pyinstaller
rm -rf dist/mac build/mac 2>/dev/null || true   # a Finder window on the folder can stop it being removed; the build overwrites it anyway
.venv/bin/pyinstaller --noconfirm --clean --log-level WARN --windowed --name DanteTest \
    --osx-bundle-identifier local.dantetest \
    --icon "$PWD/assets/DanteTest.icns" \
    --collect-all netaudio --collect-all _sounddevice_data --exclude-module PIL \
    --distpath dist/mac --workpath build/mac --specpath build/mac src/dantetest.py
APP=dist/mac/DanteTest.app
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' src/dantetest.py)
plutil -replace CFBundleShortVersionString -string "$VERSION" $APP/Contents/Info.plist
# macOS asks the user for these two permissions the first time; this is the text it shows
plutil -replace NSMicrophoneUsageDescription -string "DanteTest listens to the Dante adapter to check the audio it receives." $APP/Contents/Info.plist
plutil -replace NSLocalNetworkUsageDescription -string "DanteTest finds the Dante adapters on your network and links them." $APP/Contents/Info.plist
codesign --force --deep --sign - $APP          # ad-hoc signature, redone because Info.plist changed
rm -rf dist/mac/dmg && mkdir dist/mac/dmg
cp -R $APP dist/mac/dmg/
ln -s /Applications dist/mac/dmg/Applications
cp README.md dist/mac/dmg/README.txt
rm -f dist/DanteTest.dmg
hdiutil create -quiet -volname "DanteTest $VERSION" -srcfolder dist/mac/dmg -ov -format UDZO dist/DanteTest.dmg
ls -lh dist/DanteTest.dmg
