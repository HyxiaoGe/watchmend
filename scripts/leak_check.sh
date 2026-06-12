#!/usr/bin/env bash
# 公开仓库泄露门禁:按"形态"扫描身份/拓扑/密钥类敏感串,命中即非零退出。
# ⚠️ 个人专属明文(用户名/域名/手机号等)绝不进本文件——否则门禁脚本自己就是泄露源。
# 它们写在 scripts/leak_patterns.local(已 gitignore,每行一个 ERE,# 开头为注释)。
set -euo pipefail
cd "$(dirname "$0")/.."

PATTERNS=(
    '(^|[^0-9A-Za-z])1[3-9][0-9]{9}($|[^0-9A-Za-z])'  # 中国手机号形态(边界防误伤 hash)
    '192\.168\.[0-9]+\.'     # 内网 IP
    'cli_a[0-9a-f]{12,}'     # 飞书应用 ID
    'ou_[0-9a-f]{16,}'       # 飞书 open_id
    'oc_[0-9a-f]{16,}'       # 飞书 chat_id
    '/Users/[a-z0-9_-]+'     # macOS 家目录(本地开发机路径)
    '/home/[a-z0-9_-]+/'     # Linux 家目录(部署机路径,应参数化而非写死)
    'sk-[A-Za-z0-9]{20,}'    # API key 形态
    'hook/[0-9a-f]{8}-'      # 真实飞书 webhook token(占位 REPLACE_ME/XXXX 不命中)
)

if [ -f scripts/leak_patterns.local ]; then
    while IFS= read -r p; do
        [ -n "$p" ] && [ "${p:0:1}" != "#" ] && PATTERNS+=("$p")
    done < scripts/leak_patterns.local
fi

joined="$(IFS='|'; echo "${PATTERNS[*]}")"
if grep -rnE "$joined" . \
    --exclude-dir=.git --exclude-dir=.venv --exclude-dir=data \
    --exclude-dir=__pycache__ --exclude-dir=.pytest_cache --exclude-dir=.ruff_cache \
    --exclude=leak_check.sh --exclude=leak_patterns.local; then
    echo "leak-check: 发现敏感串(见上),禁止发布" >&2
    exit 1
fi
echo "leak-check: clean"
