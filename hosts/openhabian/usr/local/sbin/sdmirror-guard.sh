#!/bin/bash
# sdmirror-guard.sh - refuse to let openHABian's SD-mirroring jobs write to the
# wrong block device.
#
# openHABian stores the mirror target as a bare kernel name (backupdrive=/dev/sda
# in /etc/openhabian.conf) and mirror_SD() builds partition paths by string
# concat ("dd ... of=${dest}1"), so the target cannot be pinned to a stable
# /dev/disk/by-id path in the config. /dev/sda and /dev/sdb are USB probe-order
# names and can swap across a reboot or re-plug. openHABian's own "destination
# mounted" check is single-quoted in the source ('$dest[12]') and never expands,
# so it is not a real guard.
#
# This script is wired in as ExecStartPre= for sdrawcopy.service and
# sdrsync.service (see the *.service.d/guard.conf drop-ins). A non-zero exit
# aborts the unit before dd/rsync/mkfs touches anything.
#
# The openhabian-config-generated units bake the target into ExecStart:
#   ExecStart=/usr/local/sbin/mirror_SD "diff" /dev/sda   (sdrsync)
#   ExecStart=/usr/local/sbin/mirror_SD "raw"  /dev/sda   (sdrawcopy)
# so the *.service.d/guard.conf drop-ins call this as
#   ExecStartPre=/usr/local/sbin/sdmirror-guard.sh %n
# and it reads the device back out of that unit's own ExecStart - the guard can
# never end up checking a different device than the one about to be written.
#
# It also runs fine by hand:
#   sudo sdmirror-guard.sh                 # defaults to /dev/sda (what the units use)
#   sudo sdmirror-guard.sh sdrsync.service # resolve the device from a unit
#   sudo sdmirror-guard.sh /dev/sdb        # check a specific device (expects it to ABORT)
#
# If you ever replace the SD card reader, update EXPECT_SERIAL / EXPECT_BYID /
# EXPECT_MODEL below (from `udevadm info -q property -n /dev/sda` and
# `ls -l /dev/disk/by-id/`). If you replace the SSD, update SSD_SERIAL /
# SSD_MODEL.
set -euo pipefail

# --- expected mirror target: the SanDisk SDDR-B531 USB card reader ------------
EXPECT_SERIAL="1128240000005943"
EXPECT_MODEL="SDDR-B531"
EXPECT_BYID="/dev/disk/by-id/usb-SanDisk_SDDR-B531_1128240000005943-0:0"

# --- things the target must never be: the Transcend 250 GB SSD ---------------
SSD_SERIAL="I964670181"
SSD_MODEL="TS250GMTS425S"

# --- size ceiling: the mirror card is ~30 GB, the SSD is ~233 GB ------------
MAX_BYTES=$((64 * 1000 * 1000 * 1000))

fail() { echo "sdmirror-guard: ABORT - $*" >&2; exit 1; }

arg="${1:-}"
case "$arg" in
  "")
    want="/dev/sda"      # what the generated sdrsync/sdrawcopy units target
    echo "sdmirror-guard: no argument - defaulting to $want" ;;
  *.service)
    # invoked as ExecStartPre=... %n - read the device out of that unit's ExecStart
    es="$(systemctl show "$arg" -p ExecStart --value 2>/dev/null || true)"
    want="$(grep -oE '/dev/[a-z0-9/_-]+' <<<"$es" | tail -n1 || true)"
    [[ -n "$want" ]] || fail "could not extract a /dev/... target from ExecStart of $arg"
    echo "sdmirror-guard: $arg ExecStart targets $want" ;;
  /dev/*)
    want="$arg"
    echo "sdmirror-guard: checking device given on command line: $want" ;;
  *)
    fail "unrecognised argument '$arg' (expected empty, a *.service name, or /dev/...)" ;;
esac

# Resolve to a canonical device node.
target="$(readlink -f "$want" 2>/dev/null || true)"
[[ -b "$target" ]] || fail "target '$want' -> '$target' is not a block device"

props="$(udevadm info -q property -n "$target")"
serial="$(sed -n 's/^ID_SERIAL_SHORT=//p'  <<<"$props" | head -n1)"
model="$( sed -n 's/^ID_MODEL=//p'          <<<"$props" | head -n1)"
size="$(blockdev --getsize64 "$target")"

echo "sdmirror-guard: target=$target  serial='$serial'  model='$model'  size=$((size/1000/1000/1000))GB"

# 1. primary identity check - the reader's serial
[[ "$serial" == "$EXPECT_SERIAL" ]] \
  || fail "$target serial '$serial' != expected reader serial '$EXPECT_SERIAL'"

# 2. model sanity
[[ "$model" == "$EXPECT_MODEL" ]] \
  || fail "$target model '$model' != expected '$EXPECT_MODEL'"

# 3. explicit SSD blocklist
[[ "$serial" != "$SSD_SERIAL" ]] || fail "$target IS the SSD (serial $SSD_SERIAL)"
[[ "$model"  != "$SSD_MODEL"  ]] || fail "$target IS the SSD (model $SSD_MODEL)"

# 4. size ceiling
(( size <= MAX_BYTES )) \
  || fail "$target is $((size/1000/1000/1000))GB (> $((MAX_BYTES/1000/1000/1000))GB ceiling) - not the mirror card"

# 5. the expected by-id symlink must resolve to the same node
if [[ -e "$EXPECT_BYID" ]]; then
  byid_target="$(readlink -f "$EXPECT_BYID")"
  [[ "$byid_target" == "$target" ]] \
    || fail "$EXPECT_BYID -> $byid_target but target -> $target (mismatch)"
else
  fail "expected by-id path $EXPECT_BYID does not exist - reader not attached?"
fi

# 6. target must not currently back / or /mnt/ssd (findmnt walks partitions)
for mp in / /mnt/ssd /boot/firmware; do
  src="$(findmnt -no SOURCE "$mp" 2>/dev/null || true)"
  [[ -z "$src" ]] && continue
  srcdev="$(readlink -f "$src" 2>/dev/null || true)"
  # strip trailing partition suffix: /dev/sda2 -> /dev/sda, /dev/mmcblk0p2 -> /dev/mmcblk0
  srcdisk="$(lsblk -no PKNAME "$srcdev" 2>/dev/null | head -n1)"
  [[ -n "$srcdisk" ]] && srcdisk="/dev/$srcdisk"
  if [[ "$srcdev" == "$target" || "$srcdisk" == "$target" ]]; then
    fail "$target currently backs a live mount ($mp)"
  fi
done

echo "sdmirror-guard: OK - $target is the SanDisk mirror card, safe to write"
