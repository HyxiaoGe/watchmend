#!/usr/bin/env bash
# 层 2 恢复包装脚本:只重启"运行中且非 DENY"的容器。单一职责=安全边界:
# 即便上层 prompt/审批失效,本脚本能造成的最大破坏 = 重启一个非 DENY 运行容器(无数据丢失)。
# 用法: sentinel-restart.sh <容器名>
set -euo pipefail

# DENY:重启会主动有害或自毁的容器(防火墙/自动更新器/全栈反代/哨兵自身)。
# 部署者按自己环境覆盖:SENTINEL_RESTART_DENY="my-firewall my-proxy ..."(空格分隔)
read -ra DENY <<< "${SENTINEL_RESTART_DENY:-watchtower nginx-proxy dev-ops-sentinel}"

die() { echo "sentinel-restart: $1" >&2; exit "${2:-1}"; }

[ "$#" -eq 1 ] || die "需要且仅需要 1 个参数(容器名),收到 $#" 1
target="$1"

# 校验 0:容器名格式(字母数字开头,仅含字母数字与 . _ -)——挡换行/空格/元字符注入
[[ "$target" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || die "容器名 '$target' 格式非法,仅允许字母数字与 . _ -" 1

# 校验 1:目标必须是当前正在运行的容器(挡掉拼错/垃圾输入)
if ! docker ps --format '{{.Names}}' | grep -qxF -- "$target"; then
    die "容器 '$target' 不在运行中容器列表内,拒绝" 1
fi

# 校验 2:目标不在 DENY 名单
for d in "${DENY[@]}"; do
    [ "$target" = "$d" ] && die "容器 '$target' 在禁止重启名单内(防火墙/更新器/反代/哨兵自身),拒绝" 1
done

docker restart -- "$target" || die "docker restart '$target' 失败" 2
echo "sentinel-restart: 已重启 $target"
