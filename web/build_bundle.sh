#!/usr/bin/env sh
# Rebuild web/three-bundle.min.js (three.js + OrbitControls + GLTFLoader as one IIFE).
# Only needed if you want a newer three.js; the prebuilt bundle is committed.
set -e
cd "$(dirname "$0")"
npm init -y >/dev/null 2>&1 || true
npm i --no-save three esbuild
npx esbuild entry.js --bundle --minify --format=iife --outfile=three-bundle.min.js
