#!/usr/bin/env bash
# 层 2 恢复包装脚本:只重启"运行中且非 DENY"的容器。单一职责=安全边界:
# 即便上层 prompt/审批失效,本脚本能造成的最大破坏 = 重启一个非 DENY 运行容器(无数据丢失)。
# 用法: sentinel-restart.sh <容器名>
set -euo pipefail

# DENY:重启会主动有害或自毁的容器(防火墙/自动更新器/全栈反代/哨兵自身)。
# 部署者按自己环境覆盖:SENTINEL_RESTART_DENY="my-firewall my-proxy ..."(空格分隔)。
# 注意:默认值假定哨兵容器名=watchmend;若你的部署用了别的容器名,务必把它加进 DENY,
# 否则哨兵可能被指挥重启自己。
read -ra DENY <<< "${SENTINEL_RESTART_DENY:-watchtower nginx-proxy watchmend}"

die() { echo "sentinel-restart: $1" >&2; exit "${2:-1}"; }

[ "$#" -eq 1 ] || die "需要且仅需要 1 个参数(容器名),收到 $#" 1
target="$1"

# 校验 0:容器名格式(字母数字开头,仅含字母数字与 . _ -)——挡换行/空格/元字符注入
[[ "$target" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || die "容器名 '$target' 格式非法,仅允许字母数字与 . _ -" 1

# 校验 1:目标必须是当前正在运行的容器(挡掉拼错/垃圾输入)
# 先把 docker ps 输出落地再 grep:`docker ps | grep -q` 的管道里,grep -q 命中即关读端,
# producer 仍在写会吃 SIGPIPE,叠加 set -o pipefail 会把整条管道判失败 → 偶发误判"不在运行中"
# (多容器、目标靠前时尤甚)。落地后 grep 无管道、无竞态;docker ps 真失败(守护进程不可达)
# 也单独以 exit 2 报错,不被混成"grep 无匹配"。
running="$(docker ps --format '{{.Names}}')" || die "docker ps 失败,无法确认运行状态" 2
if ! grep -qxF -- "$target" <<< "$running"; then
    die "容器 '$target' 不在运行中容器列表内,拒绝" 1
fi

# 校验 2:目标不在 DENY 名单
for d in "${DENY[@]}"; do
    [ "$target" = "$d" ] && die "容器 '$target' 在禁止重启名单内(防火墙/更新器/反代/哨兵自身),拒绝" 1
done

docker restart -- "$target" || die "docker restart '$target' 失败" 2
echo "sentinel-restart: 已重启 $target"
