#!/usr/bin/env bash
# Boot a Raspberry-Pi-like ARM64 VM with QEMU for benchmarking.
#
# Uses the generic `virt` machine with a Cortex-A72 CPU (the Pi 4's core) and an
# Ubuntu arm64 cloud image. This is far faster and more practical than QEMU's
# `raspi3b`/`raspi4b` board models (no PCIe/network/large RAM there), while
# matching the Pi's CPU core, core count and memory size.
#
#   ./scripts/qemu-pi.sh            # Pi 4 defaults: 4 cores, 2 GB RAM
#   CORES=4 MEM=4G ./scripts/qemu-pi.sh
#   CORES=1 MEM=1G CPU=cortex-a53 ./scripts/qemu-pi.sh   # Pi 3 / Zero 2-ish
#
# Then:  ssh -p 2222 bench@localhost   (password: bench)
#
# Requires: qemu-system-aarch64, qemu-efi-aarch64 (UEFI firmware), qemu-utils,
#           cloud-image-utils (cloud-localds), curl.
#   sudo apt-get install qemu-system-arm qemu-efi-aarch64 qemu-utils cloud-image-utils curl
set -euo pipefail

CORES="${CORES:-4}"
MEM="${MEM:-2G}"
CPU="${CPU:-cortex-a72}"
DISK_SIZE="${DISK_SIZE:-16G}"
SSH_PORT="${SSH_PORT:-2222}"
NAME="${NAME:-rpi-${CORES}c-${MEM}}"
IMAGE_URL="${IMAGE_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img}"
VM_DIR="${VM_DIR:-$(cd "$(dirname "$0")/.." && pwd)/vm}"

mkdir -p "$VM_DIR"
cd "$VM_DIR"

base="$(basename "$IMAGE_URL")"
if [ ! -f "$base" ]; then
  echo ">> downloading $IMAGE_URL"
  curl -fL -o "$base.part" "$IMAGE_URL" && mv "$base.part" "$base"
fi

disk="$NAME.qcow2"
if [ ! -f "$disk" ]; then
  echo ">> creating overlay disk $disk ($DISK_SIZE)"
  qemu-img create -f qcow2 -F qcow2 -b "$base" "$disk" "$DISK_SIZE"
fi

if [ ! -f seed.img ]; then
  cat > user-data <<'CLOUD'
#cloud-config
hostname: dbbench-pi
users:
  - name: bench
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: false
    plain_text_passwd: bench
ssh_pwauth: true
package_update: true
packages: [git, python3, python3-venv, python3-pip, sqlite3, postgresql]
CLOUD
  echo "instance-id: dbbench-pi" > meta-data
  cloud-localds seed.img user-data meta-data
fi

FIRMWARE=""
for f in /usr/share/qemu-efi-aarch64/QEMU_EFI.fd /usr/share/AAVMF/AAVMF_CODE.fd \
         /opt/homebrew/share/qemu/edk2-aarch64-code.fd /usr/local/share/qemu/edk2-aarch64-code.fd; do
  [ -f "$f" ] && FIRMWARE="$f" && break
done
[ -n "$FIRMWARE" ] || { echo "UEFI firmware not found (install qemu-efi-aarch64)"; exit 1; }

# Hardware acceleration only works on an ARM64 host; elsewhere QEMU emulates (TCG).
ACCEL="tcg,thread=multi"
if [ "$(uname -m)" = "aarch64" ] && [ -w /dev/kvm ]; then
  ACCEL="kvm"; CPU="host"
elif [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
  ACCEL="hvf"; CPU="host"
fi

echo ">> booting $NAME: $CORES cores, $MEM RAM, cpu=$CPU, accel=$ACCEL (ssh -p $SSH_PORT bench@localhost)"
exec qemu-system-aarch64 \
  -machine virt -accel "$ACCEL" -cpu "$CPU" \
  -smp "$CORES" -m "$MEM" \
  -bios "$FIRMWARE" \
  -drive if=virtio,format=qcow2,file="$disk" \
  -drive if=virtio,format=raw,file=seed.img \
  -netdev user,id=net0,hostfwd=tcp::"$SSH_PORT"-:22 -device virtio-net-pci,netdev=net0 \
  -nographic
