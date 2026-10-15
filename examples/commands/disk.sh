#!/bin/sh
# desc: Disk usage
# phrases: disk, disk space, how much disk, диск
df -h -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null
echo
echo "Biggest folders in $HOME:"
du -xh --max-depth=1 "$HOME" 2>/dev/null | sort -rh | sed -n 2,8p
if command -v docker >/dev/null 2>&1; then
    echo
    echo "Docker:"
    docker system df 2>&1
fi
