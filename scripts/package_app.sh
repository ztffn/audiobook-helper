#!/bin/zsh
set -euo pipefail

APP_DIR="Audiobook Helper.app"
RES_DIR="$APP_DIR/Contents/Resources"
SRC_DIR="scripts"

echo "Packaging scripts into $APP_DIR…"
mkdir -p "$RES_DIR"

# Copy Python helper scripts into the app bundle
cp -f "$SRC_DIR"/audiobook_easy.py "$RES_DIR"/
cp -f "$SRC_DIR"/bootstrap_audiobook_helper.py "$RES_DIR"/
cp -f "$SRC_DIR"/concat_aac.py "$RES_DIR"/
cp -f "$SRC_DIR"/make_audiobook.py "$RES_DIR"/
cp -f "$SRC_DIR"/audiobook_pipeline.py "$RES_DIR"/ || true

echo "Scripts copied to $RES_DIR"

echo "Creating demo ZIP…"
ZIP_NAME="AudiobookHelper-demo.zip"
ditto -c -k --sequesterRsrc --keepParent "$APP_DIR" "$ZIP_NAME"
echo "Created $ZIP_NAME"

echo "Optionally, create a DMG (requires hdiutil)…"
echo "  hdiutil create -volname 'Audiobook Helper' -srcfolder '$APP_DIR' -ov -format UDZO 'AudiobookHelper.dmg'"

