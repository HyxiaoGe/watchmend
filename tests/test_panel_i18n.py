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
        "banner.issues",
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
    }
    for k in needed:
        assert k in i18n.MESSAGES["zh"], f"zh missing {k}"
        assert k in i18n.MESSAGES["en"], f"en missing {k}"
    # 带参 key 的 format 占位正确
    t = i18n.make_translator("en")
    assert t("win.label", days=30) == "30d"
    assert t("comp.expand", n=5) == "Show 5 more"
    assert "7" in t("banner.issues", open=7, probes=16)
