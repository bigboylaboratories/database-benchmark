#!/usr/bin/env bash
# Shortcut for a Raspberry Pi-like ARM64 VM. Same options as qemu-vm.sh.
#
#   ./scripts/qemu-pi.sh                 # Pi 4-like (default)
#   PRESET=pi5 ./scripts/qemu-pi.sh      # pi3 | pi4 | pi4-8g | pi5
#   CORES=2 MEM=1G ./scripts/qemu-pi.sh  # override any setting
PRESET="${PRESET:-pi4}" exec "$(dirname "$0")/qemu-vm.sh" "$@"
