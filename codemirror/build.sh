#!/bin/sh
# Builds pushraker-codemirror.js (one file, minified) and LICENSE.txt (the
# licences of everything in it) from the versions pinned in
# package-lock.json. The printer downloads that file when CodeMirror is
# installed, and checks it against the checksum in component.py
# (CODEMIRROR): rebuild, then update that.
set -e
cd "$(dirname "$0")"
npm ci --no-audit --no-fund
npx esbuild entry.js --bundle --minify --format=iife --legal-comments=none \
  --outfile=pushraker-codemirror.js
{
  echo "pushraker-codemirror.js bundles these packages, each under the MIT licence:"
  echo
  for d in $(find node_modules -name package.json -path '*node_modules/*/package.json' \
             -not -path '*/esbuild*' | sort); do
    dir=$(dirname "$d")
    lic=$(ls "$dir"/LICENSE* 2>/dev/null | head -1)
    [ -n "$lic" ] || continue
    name=$(node -p "require('./$d').name + ' ' + require('./$d').version")
    echo "==== $name"
    cat "$lic"
    echo
  done
} > LICENSE.txt
sha256sum pushraker-codemirror.js
wc -c pushraker-codemirror.js
