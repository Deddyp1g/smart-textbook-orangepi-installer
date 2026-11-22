#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SOURCE_DIR="$1/device"
STATE_DIR="$2"
IMAGE_ID="${3:-}"
BASE_ARCHIVE="${4:-}"
BASE_URL=https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/ubuntu-base-22.04.5-base-arm64.tar.gz
BASE_SHA256=075d4abd2817a5023ab0a82f5cb314c5ec0aa64a9c0b40fd3154ca3bfdae979f

if [[ -n "$IMAGE_ID" && ! "$IMAGE_ID" =~ ^sha256:[a-f0-9]{64}$ ]]; then
    echo "workspace image must be an immutable local sha256 image ID" >&2
    exit 2
fi
if [[ -n "$IMAGE_ID" && -n "$BASE_ARCHIVE" ]]; then
    echo "choose an existing workspace image ID or an Ubuntu Base archive" >&2
    exit 2
fi

BUILD_DIR="$(mktemp -d "$STATE_DIR/workspace-build.XXXXXX")"
trap 'rm -rf -- "$BUILD_DIR"' EXIT
docker() {
    env -i HOME="$BUILD_DIR" PATH=/usr/bin:/bin /usr/bin/docker --host=unix:///var/run/docker.sock "$@"
}
[[ "$(docker info --format '{{.Architecture}}')" =~ ^(aarch64|arm64)$ ]] || {
    echo "an ARM64 Docker daemon on /var/run/docker.sock is required" >&2
    exit 1
}

if [[ -z "$IMAGE_ID" ]]; then
    archive="$BUILD_DIR/ubuntu-base.tar.gz"
    if [[ -n "$BASE_ARCHIVE" ]]; then
        cp -- "$BASE_ARCHIVE" "$archive"
    else
        curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
            --connect-timeout 15 --max-time 300 --retry 2 --output "$archive" "$BASE_URL"
    fi
    printf '%s  %s\n' "$BASE_SHA256" "$archive" | sha256sum --check --status || {
        echo "Ubuntu Base archive SHA-256 mismatch" >&2
        exit 1
    }
    # Import from the verified archive every time instead of resolving a registry tag.
    # The per-build local tag keeps parallel Docker builds independent.
    base_tag="smart-textbook/orangepi-build-base:${BASE_SHA256:0:16}-${BUILD_DIR##*.}"
    trap 'docker image rm "$base_tag" >/dev/null 2>&1 || true; rm -rf -- "$BUILD_DIR"' EXIT
    docker import --platform=linux/arm64 "$archive" "$base_tag" >&2
    mkdir -- "$BUILD_DIR/context"
    # Use the same Dockerfile as the board deployment with its verified local base.
    sed "1s|^FROM smart-textbook/orangepi-cpu-base:jammy-22.04.5-arm64$|FROM $base_tag|" \
        "$SOURCE_DIR/Dockerfile" > "$BUILD_DIR/context/Dockerfile"
    [[ "$(head -n 1 "$BUILD_DIR/context/Dockerfile")" == "FROM $base_tag" ]] || {
        echo "unexpected workspace Dockerfile base" >&2
        exit 1
    }
    cp -- "$SOURCE_DIR/ascend.bashrc" "$BUILD_DIR/context/ascend.bashrc"
    docker build --platform=linux/arm64 --pull=false --iidfile "$BUILD_DIR/image-id" "$BUILD_DIR/context" >&2
    IMAGE_ID="$(<"$BUILD_DIR/image-id")"
fi

if [[ ! "$IMAGE_ID" =~ ^sha256:[a-f0-9]{64}$ ]] ||
   [[ "$(docker image inspect --format '{{.Id}} {{.Os}} {{.Architecture}} {{.Config.User}}' "$IMAGE_ID")" \
       != "$IMAGE_ID linux arm64 20000:20000" ]]; then
    echo "workspace image must be a local ARM64 Linux image with user 20000:20000" >&2
    exit 1
fi
docker run --rm --pull=never --network=none --ipc=private --read-only --cap-drop=ALL \
    --security-opt=no-new-privileges --user=20000:20000 --workdir=/home/student \
    --tmpfs /tmp:rw,nosuid,nodev,size=32m,mode=1777 \
    --tmpfs /home/student:rw,nosuid,nodev,size=32m,uid=20000,gid=20000,mode=0700 \
    --env SMART_TEXTBOOK_PROFILE=cpu --entrypoint /bin/bash "$IMAGE_ID" -ec '
        test "$(id -u)" = 20000
        test "$PWD" = /home/student
        test -r /etc/smart-textbook-ascend.bashrc
        test ! -w /usr
        test ! -e /dev/davinci0
        test ! -e /var/run/docker.sock
        test ! -e /etc/smart-textbook/runtime-agent.env
        touch "$HOME/.workspace-check"
    ' >&2
printf '%s\n' "$IMAGE_ID"
