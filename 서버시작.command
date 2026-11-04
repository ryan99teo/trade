#!/bin/sh
cd -- "$(dirname -- "$0")" || exit 1
if ! ./server start --open; then
    printf '\n오류를 확인한 후 Enter를 누르세요. '
    read -r answer
    exit 1
fi
