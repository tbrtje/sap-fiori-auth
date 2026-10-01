#!/usr/bin/env bash
# Baut zeit als eigenständiges Binary (PyInstaller, --onefile) nach dist/<ziel>/.
#
#   packaging/build.sh                      # alle Ziele
#   packaging/build.sh macos-arm64 linux-x64
#
# Ziele: macos-arm64 (nativ, nur auf einem ARM-Mac), linux-x64, linux-arm64 (Docker),
#        windows-x64 (Docker + Wine). Docker muss laufen; linux-x64 und windows-x64 laufen
#        auf einem ARM-Mac über Rosetta-Emulation.
set -euo pipefail

root="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

all=(macos-arm64 linux-x64 linux-arm64 windows-x64)
targets=("$@")
[ ${#targets[@]} -eq 0 ] && targets=("${all[@]}")

build_macos() {
  if [ "$(uname -s)-$(uname -m)" != Darwin-arm64 ]; then
    echo "Fehler: macos-arm64 lässt sich nur auf einem ARM-Mac bauen." >&2
    return 1
  fi
  uvx --python 3.12 --with requests --with truststore --with requests-gssapi --with playwright \
    pyinstaller --noconfirm --clean --onefile --name zeit --collect-submodules gssapi \
    --distpath dist/macos-arm64 --workpath build/pyinstaller --specpath build/pyinstaller \
    --log-level WARN zeit.py
}

build_docker() {
  local target="$1" platform="$2" dockerfile="$3"
  local image="zeit-build-$target"
  docker build --quiet --platform "$platform" -t "$image" -f "packaging/$dockerfile" packaging >/dev/null
  docker run --rm --platform "$platform" -v "$root":/src "$image" \
    --distpath "dist/$target" --log-level WARN zeit.py
}

for target in "${targets[@]}"; do
  echo "==> $target" >&2
  rm -rf "dist/$target"
  case "$target" in
    macos-arm64) build_macos ;;
    linux-x64) build_docker "$target" linux/amd64 linux.Dockerfile ;;
    linux-arm64) build_docker "$target" linux/arm64 linux.Dockerfile ;;
    windows-x64) build_docker "$target" linux/amd64 windows.Dockerfile ;;
    *) echo "Fehler: unbekanntes Ziel '$target' (erlaubt: ${all[*]})" >&2; exit 1 ;;
  esac
done

# Prüfsummen über alle vorhandenen Binaries, im Format von sha256sum/shasum -c.
(cd dist && shopt -s nullglob && bins=(*/zeit */zeit.exe) && shasum -a 256 "${bins[@]}" > SHA256SUMS)
echo "Fertig:" >&2
cat dist/SHA256SUMS >&2
