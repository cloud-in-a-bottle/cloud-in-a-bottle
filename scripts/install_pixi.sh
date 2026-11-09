#!/usr/bin/env bash
set -euo pipefail

version=${1:?Usage: install_pixi.sh VERSION}
version=${version#v}
if [[ $(uname -s) != Linux ]]; then
    echo "This installer supports Linux hosts only." >&2
    exit 2
fi
arch=$(uname -m)
case "$arch" in
    arm64) arch=aarch64 ;;
esac

# SHA256 digests of the vendor's release archives. Keep the older version for
# migration tests and update the production pin and its two digests together.
case "$version/$arch" in
    0.70.2/x86_64) sha256=9d0d72fc1fa1a8f87bfde943cf25d3575cb352cb516795d4dab21898bd98adea ;;
    0.70.2/aarch64) sha256=19f61d069f7ab963f3b9d6794134f893c83ecd2b7cd698e19268daa3a1fa606b ;;
    0.69.0/x86_64) sha256=285e021a8dd81d151e1dd03541ef6525a8205e0e6422aae3dfe17772b20cdd41 ;;
    0.69.0/aarch64) sha256=8e05e2f3c79bcfdde5656d6e021062a2d1275bd0b4115360bef4560fc3fb1974 ;;
    *) echo "No pinned Pixi archive for $version/$arch" >&2; exit 2 ;;
esac

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bin_dir=${PIXI_BIN_DIR:-$HOME/.pixi/bin}
cache_dir=${PIXI_CACHE_DIR:-$HOME/.cache/openhost/pixi}
asset="pixi-${arch}-unknown-linux-musl.tar.gz"
archive="$cache_dir/$version-$asset"
bash "$script_dir/download_verified.sh" \
    "https://github.com/prefix-dev/pixi/releases/download/v$version/$asset" "$sha256" "$archive"

mkdir -p "$bin_dir"
stage=$(mktemp -d "$bin_dir/.pixi-install.XXXXXX")
trap 'rm -rf "$stage"' EXIT
tar -xzf "$archive" -C "$stage" pixi
chmod 0755 "$stage/pixi"
if [[ $("$stage/pixi" --version) != "pixi $version" ]]; then
    echo "Downloaded Pixi does not report version $version" >&2
    exit 1
fi
mv -fT "$stage/pixi" "$bin_dir/pixi"
