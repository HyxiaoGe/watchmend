from sentinel.findings import RULE_NAMES
from sentinel.panel import i18n


def test_resolve_lang_priority():
    # query 优先
    assert i18n.resolve_lang("en", "zh", "zh-CN,zh;q=0.9") == "en"
    assert i18n.resolve_lang("ZH", "en", None) == "zh"  # 大小写不敏感
    # cookie 次之
    assert i18n.resolve_lang(None, "en", "zh-CN") == "en"
    # Accept-Language：zh* → zh，否则 en
    assert i18n.resolve_lang(None, None, "zh-CN,zh;q=0.9") == "zh"
    assert i18n.resolve_lang(None, None, "en-US,en;q=0.9") == "en"
    assert i18n.resolve_lang(None, None, "fr-FR") == "en"  # 非 zh 的 Accept → en
    # 无任何信号 → 默认 zh
    assert i18n.resolve_lang(None, None, None) == "zh"
    # 非法 query 落到下一优先级（此处无 cookie/accept → 默认 zh）
    assert i18n.resolve_lang("bogus", None, None) == "zh"


def test_resolve_lang_configured_default():
    # 配置默认(zh|en)优先级:在 query/cookie 之后、Accept-Language 之前
    assert i18n.resolve_lang(None, None, "zh-CN", default="en") == "en"  # 默认覆盖浏览器
    assert i18n.resolve_lang("zh", None, None, default="en") == "zh"  # query 仍最优先
    assert i18n.resolve_lang(None, "zh", None, default="en") == "zh"  # cookie 优先于默认
    assert i18n.resolve_lang(None, None, "zh-CN", default="") == "zh"  # 空默认=auto→Accept
    assert i18n.resolve_lang(None, None, "en-US", default="bogus") == "en"  # 非法默认忽略→Accept
    assert i18n.resolve_lang(None, None, None, default="en") == "en"  # 无信号时用默认
    assert i18n.resolve_lang(None, None, None) == "zh"  # 不传 default 行为不变


def test_make_translator_lookup_and_fallback():
    t_en = i18n.make_translator("en")
    assert t_en("st.ok") == "Operational"
    assert t_en("missing.key.xyz") == "missing.key.xyz"  # 缺失 → 返回 key 本身
    t_zh = i18n.make_translator("zh")
    assert t_zh("st.ok") == "正常"
    # 未知 lang 退回 zh 表
    assert i18n.make_translator("de")("st.ok") == "正常"


def test_make_translator_format_kwargs():
    t = i18n.make_translator("en")
    assert t("axis.window", days=30) == "30 days ago"
    assert i18n.make_translator("zh")("axis.window", days=30) == "30 天前"


def test_rule_labels_cover_all_findings_rules():
    for rule in RULE_NAMES:
        assert rule in i18n.RULE_LABELS, f"RULE_LABELS missing {rule}"
        assert set(i18n.RULE_LABELS[rule]) >= {"zh", "en"}
    assert i18n.rule_label("service_down", "en") == "Service down"
    assert i18n.rule_label("service_down", "zh") == "服务异常"
    assert i18n.rule_label("not_a_rule", "en") == "not_a_rule"  # 未知 → 原样


def test_messages_zh_en_parity():
    # 两语言 key 集必须完全一致(防补一边漏一边)
    assert set(i18n.MESSAGES["zh"]) == set(i18n.MESSAGES["en"])


def test_new_template_keys_present():
    needed = {
        "ev.diag_lang",
        "llm.pending_restart",
        "win.label",
        "comp.uptime",
        "comp.expand",
        "comp.collapse",
        "host.host",
        "host.self",
        "host.host.ok",
        "ev.ai",
        "ev.summary",
        "ev.detail",
        "ev.scanfail",
        "footer.notify",
        "footer.none",
        "footer.docker_readonly",
        "footer.env",
        "ev.notfound",
        "ev.notfound_hint",
        "ev.commands",
    }
    for k in needed:
        assert k in i18n.MESSAGES["zh"], f"zh missing {k}"
        assert k in i18n.MESSAGES["en"], f"en missing {k}"
    # 带参 key 的 format 占位正确
    t = i18n.make_translator("en")
    assert t("win.label", days=30) == "30d"
    assert t("comp.expand", n=5) == "Show 5 more"
    assert "back-translated" in t("ev.diag_lang", lang="zh")


def test_crashloop_rule_registered():
    from sentinel import findings
    from sentinel.panel import i18n

    assert findings.RULE_NAMES.get("container_crashloop") == "容器频繁重启"
    # point 事件,镜像 container_oom:进 DOCKER_RULES(成功评估时进 scope),不进 OPEN 集
    assert "container_crashloop" in findings.DOCKER_RULES
    assert "container_crashloop" not in findings.DOCKER_OPEN_RULES
    assert i18n.RULE_LABELS["container_crashloop"]["zh"] == "容器频繁重启"
    assert i18n.RULE_LABELS["container_crashloop"]["en"] == "Container crash-looping"


def test_phase1_panel_keys_present_both_langs():
    from sentinel.panel.i18n import MESSAGES

    keys = [
        "tab.overview",
        "tab.services",
        "tab.events",
        "tab.hygiene",
        "hero.uptime_label",
        "hero.days_clean",
        "hero.open",
        "hero.no_events",
        "sec.services",
    ]
    for lang in ("zh", "en"):
        for k in keys:
            assert k in MESSAGES[lang], f"{lang} 缺 key {k}"


def test_phase1_format_keys_accept_params():
    from sentinel.panel.i18n import make_translator

    t = make_translator("zh")
    assert t("hero.uptime_label") == "今日可用率"  # 今日口径,无窗口占位
    assert "5" in t("hero.days_clean", n=5)
    assert "2" in t("hero.open", open=2)
