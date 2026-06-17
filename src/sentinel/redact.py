"""运行时日志脱敏:自由文本工具输出(docker_logs/loki_logs)在喂 LLM 和落证据链前
scrub 密钥。

两层:
  ① 已知密钥字面值精确匹配(secrets,零误伤):WatchMend 自有密钥 + 目标容器自有 env 值。
  ② 保守通用密钥形态正则(use_patterns=True,仅日志类工具):只匹配高置信形态,
     避免误伤正常日志(hash/ID/请求参数)。

与 scripts/leak_check.sh 区别:那是发布门禁,扫"开发者本人"的身份/拓扑(手机号/内网 IP/
家目录)防其漏进仓库;本模块面向"用户容器日志"里的通用密钥,二者各管各的。

关键纪律:密钥绝不能发给外部 LLM(出内网),故脱敏在喂模型之前完成——模型与证据链拿到
同一份脱敏后文本(区别于截断:截断只截存储副本、模型拿全文)。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

PLACEHOLDER = "«redacted»"
# 精确匹配的最短密钥:更短的串(罕见)做全局子串替换风险大于收益(会把满屏正常子串脱掉)。
_MIN_SECRET_LEN = 6

# 高熵密钥形态(独立成形,无需关键字上下文)。前缀/定界明确,误伤可忽略。
_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),  # OpenAI/LLM 风格 key
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub 经典 token
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),  # GitHub 细粒度 PAT(前缀含下划线,与上条不同)
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack token
    re.compile(r"AIza[0-9A-Za-z_\-]{35,}"),  # Google/GCP API key(AIza+35 起,固定 39 字符形态)
)

# PEM 私钥块:BEGIN..END 之间整段脱掉(单独成则因需 DOTALL 跨行)。
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

# 值需含数字的前瞻:真密钥/令牌几乎必含数字,而 Bearer/赋值后的英文诊断词(credentials/
# expired_signature/invalid_token)不含——以此保守区分"凭据"与"诊断文本",宁漏勿误伤
# (纯字母密钥会漏,但①层精确匹配兜已知密钥,符合既定保守姿态)。前瞻字符类必须与各自的值
# 字符类一致,否则数字前有越界字符(如 P@ss0rd 的 @)会让前瞻提前断裂、漏掉真密码。

# Bearer/Basic <credential>:保留 scheme 词,只脱凭据(凭据须含数字,避免吞英文词)。
_BEARER = re.compile(r"(?i)\b(bearer|basic)(\s+)((?=[A-Za-z0-9._\-+/=]*\d)[A-Za-z0-9._\-+/=]{8,})")

# 显式赋值 key=value / key: value / "key":"value":必须有 =/: 分隔(保守——不脱 keyword+
# 空格+普通词),keyword 门控提高置信度;分隔符两侧放行可选引号以覆盖 JSON 日志行。保留键名
# 与分隔符,只脱值;值须含数字(避免误脱结构化日志里 keyword 后的英文诊断词)。authorization
# 头交 _BEARER 专管(其值多为 Bearer/Basic <cred>,留在此处会把 scheme 词也当值脱掉)。
_ASSIGN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|client[_-]?secret|secret[_-]?key"
    r"|private[_-]?key|credentials?|token|password|passwd|pwd|secret)"
    r"(\"?\s*[:=]\s*\"?)"
    r"((?=[^\s\"]*\d)[^\s\"]{4,})"
)

# 连接串密码 scheme://user:pass@host:保留 scheme/user/host,只脱 pass。用户名可空
# (redis://:pass@ 是 redis-py 默认形态);密码段放行 / 在 @ 处收尾(含 / 的强口令不漏)。
# 三段全部收界(ReDoS 防护):无界量词在超长无空白 alnum 串上 re.sub 逐起点贪婪扫+回溯
# 呈 O(n²),同步阻塞 asyncio 事件循环。scheme≤20(最长真实 scheme mongodb+srv=11)、
# user≤127、pass≤512 远超任何真实连接串,逐起点工作量封顶→整体线性。
_CONNSTR = re.compile(r"([a-z][a-z0-9+.\-]{0,19}://[^\s:/@]{0,127}:)([^\s@]{1,512})(@)")


def _apply_patterns(text: str) -> tuple[str, int]:
    counter = {"n": 0}

    def _full(_m: re.Match[str]) -> str:
        counter["n"] += 1
        return PLACEHOLDER

    def _keep_prefix(*groups: int):
        def _r(m: re.Match[str]) -> str:
            counter["n"] += 1
            return "".join(m.group(g) for g in groups) + PLACEHOLDER

        return _r

    def _connstr(m: re.Match[str]) -> str:  # 占位符在中间,不能用 _keep_prefix
        counter["n"] += 1
        return m.group(1) + PLACEHOLDER + m.group(3)

    out = text
    out = _PEM.sub(_full, out)  # 先脱整块 PEM,免得块内 base64 行被其他规则零散误伤
    for pat in _SHAPE_PATTERNS:
        out = pat.sub(_full, out)
    out = _BEARER.sub(_keep_prefix(1, 2), out)  # 保留 "Bearer" + 空白
    out = _ASSIGN.sub(_keep_prefix(1, 2), out)  # 保留 键名 + "=/: " 分隔
    out = _CONNSTR.sub(_connstr, out)  # 保留 scheme/user/host,只脱密码段
    return out, counter["n"]


def redact_text(
    text: str, *, secrets: Iterable[str] = (), use_patterns: bool = False
) -> tuple[str, int]:
    """脱敏 text,返回 (脱敏后文本, 脱敏次数)。

    secrets:已知密钥字面值,精确子串替换(零误伤);短于 _MIN_SECRET_LEN 的忽略。
    use_patterns:是否额外跑保守通用形态正则(日志类工具用 True)。
    """
    out = text
    count = 0
    # 长串优先:防止较短密钥先替换、破坏较长密钥的匹配。
    uniq = sorted({s for s in secrets if s and len(s) >= _MIN_SECRET_LEN}, key=len, reverse=True)
    for s in uniq:
        hits = out.count(s)
        if hits:
            out = out.replace(s, PLACEHOLDER)
            count += hits
    if use_patterns:
        out, n = _apply_patterns(out)
        count += n
    return out, count
