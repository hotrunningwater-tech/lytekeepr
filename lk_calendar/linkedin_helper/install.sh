#!/bin/sh
# Installs the LyteKeepr LinkedIn helper on a Mac, in the person's own home folder (no administrator password).
set -eu
ADDRESS="__ADDRESS__"
PACKAGE="__PACKAGE__"
CODE="${1:-}"
DIR="$HOME/.lytekeepr-linkedin"
PLIST="$HOME/Library/LaunchAgents/xyz.lytekeepr.linkedin.plist"

if [ "$(uname)" != "Darwin" ]; then echo "This installer is for Mac. On Windows use the Windows line in LyteKeepr."; exit 1; fi
if [ -z "$CODE" ]; then echo "The connection code is missing: copy the whole line from LyteKeepr (LinkedIn tab)."; exit 1; fi
mkdir -p "$DIR/bin" "$HOME/Library/LaunchAgents"

echo "1 of 4: getting the helper..."
if command -v uv >/dev/null 2>&1; then UV="$(command -v uv)"; else
  curl -LsSf https://astral.sh/uv/__UV_VERSION__/install.sh | env UV_UNMANAGED_INSTALL="$DIR/bin" sh >/dev/null
  UV="$DIR/bin/uv"
fi
curl -fsSL "$ADDRESS/linkedin-helper/agent.py" -o "$DIR/agent.py"

echo "2 of 4: connecting to your workspace..."
"$UV" run --quiet --script "$DIR/agent.py" pair "$CODE" "$ADDRESS"

echo "3 of 4: a browser window opens. Log in to LinkedIn there, then this continues by itself."
"$UV" tool run --quiet --from "$PACKAGE" linkedin-mcp profile create >/dev/null 2>&1 || true
"$UV" tool run --quiet --from "$PACKAGE" linkedin-mcp setup >/dev/null
"$UV" tool run --quiet --from "$PACKAGE" linkedin-mcp login >/dev/null

echo "4 of 4: starting the helper..."
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>xyz.lytekeepr.linkedin</string>
  <key>ProgramArguments</key><array><string>$UV</string><string>run</string><string>--quiet</string><string>--script</string><string>$DIR/agent.py</string><string>tick</string></array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/helper.log</string>
  <key>StandardErrorPath</key><string>$DIR/helper.log</string>
</dict></plist>
PLIST
launchctl remove xyz.lytekeepr.linkedin 2>/dev/null || true
launchctl load "$PLIST"
echo "Done. Your LinkedIn messages will appear in LyteKeepr. You can close this window."
