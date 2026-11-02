#!/vendor/bin/sh
#
# Snapshot the pstore/bootloader logs of the previous boot into /data so they
# survive a clean install, a power-off or a dead userspace.

KLOG_DIR=/data/misc/logs/klog
MAX_BOOTS=9

mkdir -p "$KLOG_DIR"
chown system log "$KLOG_DIR"
chmod 0770 "$KLOG_DIR"

# Advance the boot counter and drop the entry it wraps over.
count_file="$KLOG_DIR/.count"
slot=1
if [ -r "$count_file" ]; then
    slot=$(cat "$count_file")
    case "$slot" in
        ''|*[!0-9]*) slot=1 ;;
    esac
    slot=$((slot + 1))
fi
if [ "$slot" -gt "$MAX_BOOTS" ]; then
    slot=1
fi
echo "$slot" > "$count_file"
chown system log "$count_file"

prev="$KLOG_DIR/prev-$slot"
rm -rf "$prev"
mkdir -p "$prev"
chown system log "$prev"
chmod 0770 "$prev"

# pstore records written by the boot that just ended.
for f in /sys/fs/pstore/console-ramoops* \
         /sys/fs/pstore/dmesg-ramoops* \
         /sys/fs/pstore/ftrace-ramoops* \
         /sys/fs/pstore/pmsg-ramoops*; do
    [ -e "$f" ] || continue
    cat "$f" > "$prev/$(basename "$f")" 2>/dev/null
done

# Firmware log of this boot, captured by the kernel before it could be
# overwritten. Useful when the previous boot died in the bootloader.
if [ -r /proc/bootloader_log ]; then
    cat /proc/bootloader_log > "$prev/xbl.log" 2>/dev/null
fi

# Snapshot the kernel wrote to persist before the previous boot died, even if
# it was a power-off that took DRAM with it. Located before persist/klog is
# overwritten by this boot's flush.
if [ -r /proc/last_kmsg ]; then
    cat /proc/last_kmsg > "$prev/last_kmsg.txt" 2>/dev/null
fi

dmesg > "$prev/dmesg.txt" 2>/dev/null

# Compress the older snapshots, keep the current boot's logs plain.
for d in "$KLOG_DIR"/prev-*; do
    [ -d "$d" ] || continue
    [ "$d" = "$prev" ] && continue
    gzip -f "$d"/* 2>/dev/null
done

chown -R system log "$KLOG_DIR"
