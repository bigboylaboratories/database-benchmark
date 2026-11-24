#!/usr/bin/env bash
# Boot a Linux VM with QEMU for benchmarking: normal x86_64 servers or ARM64
# (Raspberry Pi-like) boards, with any number of cores, RAM, CPU limit and disk speed.
#
#   PRESET=x86-4c ./scripts/qemu-vm.sh               # 4-core / 4 GB x86_64 Linux server
#   PRESET=pi4    ./scripts/qemu-vm.sh               # Raspberry Pi 4-like ARM64 board
#   ARCH=x86_64 CORES=6 MEM=12G ./scripts/qemu-vm.sh # any custom machine
#   PRESET=x86-4c CPU_LIMIT=200% ./scripts/qemu-vm.sh   # 4 vCPUs but only 2 cores' worth of host time
#   PRESET=x86-2c DISK_MBPS=25 DISK_IOPS=500 ./scripts/qemu-vm.sh  # slow disk
#
# Then:  ssh -p 2222 bench@localhost   (password: bench)
#
# Settings (environment variables; a PRESET fills in defaults, anything you set wins):
#   ARCH       x86_64 | aarch64                   (default x86_64)
#   CORES      vCPU count                          MEM        RAM, e.g. 512M, 4G
#   CPU        QEMU CPU model (default: host with KVM, else max / cortex-a72)
#   CPU_LIMIT  cap on host CPU time, e.g. 50% or 250% (needs systemd-run)
#   PIN        pin the VM to host cores, e.g. 0-3 or 2,3 (taskset); avoids noisy scheduling
#   DISK_MBPS  disk bandwidth cap in MB/s          DISK_IOPS  disk IOPS cap
#   DISK_SIZE  virtual disk size (default 16G)     SSH_PORT   host port for SSH (default 2222)
#   NAME       VM name = disk file in vm/ and the guest hostname (default derived from settings)
#   NO_KVM=1   force software emulation even if KVM is available
#
# Host: Linux. Install:
#   sudo apt-get install qemu-system-x86 qemu-system-arm qemu-efi-aarch64 qemu-utils cloud-image-utils curl
set -euo pipefail

# ----------------------------------------------------------------- presets -- #
# Format: ARCH CPU CORES MEM [DISK_MBPS DISK_IOPS]
preset() {
  case "$1" in
    # Generic Linux servers / desktops (x86_64, CPU picked automatically)
    x86-1c)  echo "x86_64 auto 1 1G" ;;
    x86-2c)  echo "x86_64 auto 2 2G" ;;
    x86-4c)  echo "x86_64 auto 4 4G" ;;
    x86-8c)  echo "x86_64 auto 8 8G" ;;
    x86-16c) echo "x86_64 auto 16 16G" ;;
    # Generic ARM64 Linux servers (e.g. cloud Graviton/Ampere-style)
    arm-2c)  echo "aarch64 auto 2 2G" ;;
    arm-4c)  echo "aarch64 auto 4 4G" ;;
    arm-8c)  echo "aarch64 auto 8 8G" ;;
    # Raspberry Pi-like boards (SD-card-ish disk limits; DISK_MBPS= DISK_IOPS= removes them)
    pi3)     echo "aarch64 cortex-a53 4 1G 20 1500" ;;
    pi4)     echo "aarch64 cortex-a72 4 2G 40 2500" ;;
    pi4-8g)  echo "aarch64 cortex-a72 4 8G 40 2500" ;;
    pi5)     echo "aarch64 cortex-a76 4 4G 80 4000" ;;
    *) echo "unknown PRESET '$1' (x86-1c x86-2c x86-4c x86-8c x86-16c arm-2c arm-4c arm-8c pi3 pi4 pi4-8g pi5)" >&2
       exit 1 ;;
  esac
}

if [ -n "${PRESET:-}" ]; then
  read -r p_arch p_cpu p_cores p_mem p_mbps p_iops <<<"$(preset "$PRESET")"
  ARCH="${ARCH:-$p_arch}"
  CORES="${CORES:-$p_cores}"
  MEM="${MEM:-$p_mem}"
  [ "$p_cpu" != "auto" ] && CPU="${CPU:-$p_cpu}"
  DISK_MBPS="${DISK_MBPS-${p_mbps:-}}"
  DISK_IOPS="${DISK_IOPS-${p_iops:-}}"
fi

ARCH="${ARCH:-x86_64}"
CORES="${CORES:-2}"
MEM="${MEM:-2G}"
DISK_SIZE="${DISK_SIZE:-16G}"
SSH_PORT="${SSH_PORT:-2222}"
DISK_MBPS="${DISK_MBPS:-}"
DISK_IOPS="${DISK_IOPS:-}"
CPU_LIMIT="${CPU_LIMIT:-}"
PIN="${PIN:-}"
VM_DIR="${VM_DIR:-$(cd "$(dirname "$0")/.." && pwd)/vm}"

case "$ARCH" in
  x86_64|amd64)  ARCH=x86_64; IMG_ARCH=amd64 ;;
  aarch64|arm64) ARCH=aarch64; IMG_ARCH=arm64 ;;
  *) echo "ARCH must be x86_64 or aarch64" >&2; exit 1 ;;
esac
IMAGE_URL="${IMAGE_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-${IMG_ARCH}.img}"

# KVM only when guest arch == host arch; otherwise QEMU emulates (TCG, much slower).
ACCEL="tcg,thread=multi"
if [ "${NO_KVM:-0}" != "1" ] && [ "$(uname -m)" = "$ARCH" ] && [ -w /dev/kvm ]; then
  ACCEL="kvm"
fi
if [ -z "${CPU:-}" ]; then
  if [ "$ACCEL" = "kvm" ]; then CPU=host
  elif [ "$ARCH" = "x86_64" ]; then CPU=max
  else CPU=cortex-a72
  fi
fi

# Name encodes the config so each machine gets its own disk unless you pin NAME.
default_name="${PRESET:-$ARCH-${CORES}c-${MEM}}"
[ -n "$CPU_LIMIT" ] && default_name="$default_name-cpu${CPU_LIMIT%\%}"
[ -n "$DISK_MBPS" ] && default_name="$default_name-${DISK_MBPS}mbps"
[ -n "$DISK_IOPS" ] && default_name="$default_name-${DISK_IOPS}iops"
NAME="${NAME:-$default_name}"
NAME="$(printf '%s' "$NAME" | sed 's/[^A-Za-z0-9-]/-/g' | tr 'A-Z' 'a-z')"

mkdir -p "$VM_DIR"
cd "$VM_DIR"

base="$(basename "$IMAGE_URL")"
if [ ! -f "$base" ]; then
  echo ">> downloading $IMAGE_URL"
  curl -fL -o "$base.part" "$IMAGE_URL" && mv "$base.part" "$base"
fi

disk="$NAME.qcow2"
if [ ! -f "$disk" ]; then
  echo ">> creating disk $disk ($DISK_SIZE, backed by $base)"
  qemu-img create -f qcow2 -F qcow2 -b "$base" "$disk" "$DISK_SIZE"
fi

seed="$NAME-seed.img"
if [ ! -f "$seed" ]; then
  cat > "$NAME-user-data" <<CLOUD
#cloud-config
hostname: $NAME
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
  echo "instance-id: $NAME" > "$NAME-meta-data"
  cloud-localds "$seed" "$NAME-user-data" "$NAME-meta-data"
fi

throttle=""
[ -n "$DISK_MBPS" ] && throttle="$throttle,throttling.bps-total=$((DISK_MBPS * 1024 * 1024))"
[ -n "$DISK_IOPS" ] && throttle="$throttle,throttling.iops-total=$DISK_IOPS"

if [ "$ARCH" = "x86_64" ]; then
  QEMU=(qemu-system-x86_64 -machine q35)
else
  FIRMWARE=""
  for f in /usr/share/qemu-efi-aarch64/QEMU_EFI.fd /usr/share/AAVMF/AAVMF_CODE.fd; do
    [ -f "$f" ] && FIRMWARE="$f" && break
  done
  [ -n "$FIRMWARE" ] || { echo "UEFI firmware not found (install qemu-efi-aarch64)" >&2; exit 1; }
  QEMU=(qemu-system-aarch64 -machine virt -bios "$FIRMWARE")
fi

QEMU+=(
  -accel "$ACCEL" -cpu "$CPU" -smp "$CORES" -m "$MEM"
  -drive "if=virtio,format=qcow2,file=$disk$throttle"
  -drive "if=virtio,format=raw,file=$seed"
  -netdev "user,id=net0,hostfwd=tcp::$SSH_PORT-:22" -device virtio-net-pci,netdev=net0
  -nographic
)

# Wrap QEMU to limit / pin host CPU.
WRAP=()
if [ -n "$CPU_LIMIT" ]; then
  command -v systemd-run >/dev/null || { echo "CPU_LIMIT needs systemd-run" >&2; exit 1; }
  WRAP+=(systemd-run --user --scope -q -p "CPUQuota=$CPU_LIMIT")
fi
[ -n "$PIN" ] && WRAP+=(taskset -c "$PIN")

echo ">> booting '$NAME': $ARCH, $CORES vCPU, $MEM RAM, cpu=$CPU, accel=${ACCEL%%,*}" \
     "${CPU_LIMIT:+, cpu limit $CPU_LIMIT}${PIN:+, pinned to host cores $PIN}" \
     "${DISK_MBPS:+, disk ${DISK_MBPS}MB/s}${DISK_IOPS:+, ${DISK_IOPS} IOPS}"
echo ">> ssh -p $SSH_PORT bench@localhost  (password: bench)   quit QEMU: Ctrl-A then X"
exec "${WRAP[@]}" "${QEMU[@]}"
