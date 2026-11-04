#!/bin/sh
cd -- "$(dirname -- "$0")" || exit 1
sh ./export.sh
result=$?
if [ "$result" -ne 0 ]; then
    echo '导出未完成，请查看上方提示。'
fi
exit "$result"
