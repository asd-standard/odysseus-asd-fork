#!/usr/bin/env bash
# Build patched wheels for Real-ESRGAN's unmaintained dependencies.
#
# basicsr / gfpgan / facexlib (xinntao, last released 2022) read their version
# in setup.py with:
#
#     exec(compile(f.read(), version_file, 'exec'))
#     return locals()['__version__']
#
# Python 3.13+ implements PEP 667: locals() inside a function returns an
# independent snapshot that exec() can no longer mutate, so the read raises
# `KeyError: '__version__'` and the sdist build fails. That is why the Cookbook
# "install realesrgan" button dies on the python:3.14 image. The packages have
# no fixed release, so we patch get_version() to exec into an explicit namespace
# dict (works on every Python) and build wheels from the patched source.
#
# basicsr additionally declares setup_requires=['cython', 'numpy', 'torch'],
# which makes pip's build isolation download a multi-GB torch wheel just to
# build a pure-Python wheel (BASICSR_EXT is unset, so there are no C
# extensions). We drop torch from the build-time deps. The wheel's runtime
# metadata is unchanged, so realesrgan still pulls torch when it is installed.
#
# Usage: build-realesrgan-wheels.sh [OUTPUT_DIR]   (default: /wheels)
set -euo pipefail

OUT="${1:-/wheels}"
mkdir -p "$OUT"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"

# Pinned to the versions Real-ESRGAN 0.3.0 resolves to.
SPECS="basicsr==1.4.2 gfpgan==1.3.8 facexlib==0.3.0"

for spec in $SPECS; do
  name="${spec%%==*}"
  ver="${spec##*==}"
  # pip download builds metadata (and trips the same bug), so fetch the raw
  # sdist URL from the PyPI JSON API instead.
  url="$(python - "$name" "$ver" <<'PY'
import json, sys, urllib.request
name, ver = sys.argv[1], sys.argv[2]
data = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{ver}/json"))
for f in data["urls"]:
    if f["packagetype"] == "sdist":
        print(f["url"]); break
else:
    sys.exit(f"no sdist found for {name}=={ver}")
PY
)"
  echo ">> fetching ${name} ${ver}: ${url}"
  curl -fsSL "$url" -o "${name}.tar.gz"
  tar xzf "${name}.tar.gz"
done

echo ">> patching get_version() and build-time setup_requires"
python - <<'PY'
import pathlib

old_exec = "exec(compile(f.read(), version_file, 'exec'))"
new_exec = "_ver_ns = {}\n        exec(compile(f.read(), version_file, 'exec'), _ver_ns)"
old_ret = "return locals()['__version__']"
new_ret = "return _ver_ns['__version__']"
old_setup_requires = "setup_requires=['cython', 'numpy', 'torch'],"
new_setup_requires = "setup_requires=['cython', 'numpy'],"

patched_version = 0
patched_setup_requires = 0
for setup in pathlib.Path(".").glob("*/setup.py"):
    s = setup.read_text()
    if old_exec in s and old_ret in s:
        s = s.replace(old_exec, new_exec).replace(old_ret, new_ret)
        patched_version += 1
        print("   patched get_version():", setup)
    if old_setup_requires in s:
        s = s.replace(old_setup_requires, new_setup_requires)
        patched_setup_requires += 1
        print("   patched setup_requires:", setup)
    setup.write_text(s)

assert patched_version == 3, f"expected to patch 3 setup.py files, patched {patched_version}"
assert patched_setup_requires == 1, (
    f"expected basicsr setup_requires patch, patched {patched_setup_requires}"
)
PY

echo ">> building wheels into ${OUT}"
pip wheel --no-deps -w "$OUT" ./basicsr-* ./gfpgan-* ./facexlib-*
ls -l "$OUT"
