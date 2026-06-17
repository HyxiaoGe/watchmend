import time

from sentinel.redact import PLACEHOLDER, redact_text


def test_exact_secret_redacted_zero_false_positive():
    out, n = redact_text(
        "config loaded; key=SUPERSECRETVALUE123 ok", secrets=["SUPERSECRETVALUE123"]
    )
    assert "SUPERSECRETVALUE123" not in out
    assert PLACEHOLDER in out
    assert n == 1


def test_exact_secret_multiple_occurrences_counted():
    out, n = redact_text("abc TOKENXYZ123 def TOKENXYZ123", secrets=["TOKENXYZ123"])
    assert "TOKENXYZ123" not in out
    assert n == 2


def test_short_secret_below_min_ignored_to_avoid_mass_redaction():
    # 过短的"密钥"不参与精确匹配,避免把满屏正常子串脱掉。
    out, n = redact_text("the cat sat on the mat", secrets=["cat"])
    assert out == "the cat sat on the mat"
    assert n == 0


def test_patterns_off_by_default_no_regex_scrub():
    # 默认只精确匹配,不跑形态正则(供非日志工具用,零误伤)。
    text = "calling sk-abcdef-ABCDEF-0123456789-xyz now"
    out, n = redact_text(text, secrets=[])
    assert out == text
    assert n == 0


def test_sk_apikey_shape_redacted_when_patterns_on():
    out, n = redact_text(
        "openai sk-abcdef-ABCDEF-0123456789-xyz end", secrets=[], use_patterns=True
    )
    assert "sk-abcdef-ABCDEF-0123456789-xyz" not in out
    assert n >= 1


def test_jwt_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk"
    out, n = redact_text(f"auth {jwt} end", secrets=[], use_patterns=True)
    assert jwt not in out
    assert n >= 1


def test_keyvalue_assignment_redacts_value_keeps_key():
    out, n = redact_text("db connect password=hunter2secret host=db", secrets=[], use_patterns=True)
    assert "hunter2secret" not in out
    assert "password" in out  # 键名保留,便于诊断
    assert "host=db" in out  # 非密钥关键字的赋值不动


def test_conservative_no_false_positive_on_keyword_plus_plain_word():
    # 保守:keyword 后无 =/: 赋值分隔符 → 不脱(避免误伤正常英文)。
    text = "password changed successfully and token refreshed"
    out, n = redact_text(text, secrets=[], use_patterns=True)
    assert out == text
    assert n == 0


def test_bearer_token_redacted_scheme_word_kept():
    out, n = redact_text("Authorization: Bearer abcDEF123456ghiJKL", secrets=[], use_patterns=True)
    assert "abcDEF123456ghiJKL" not in out
    assert "Bearer" in out
    assert n >= 1


def test_connstring_password_redacted_user_and_host_kept():
    out, n = redact_text(
        "dsn postgres://appuser:p4ssw0rd@db:5432/app", secrets=[], use_patterns=True
    )
    assert "p4ssw0rd" not in out
    assert "appuser" in out
    assert "db:5432" in out


def test_host_port_path_not_false_positive():
    # scheme://host:port/path 无凭据 → 连接串规则不应误伤。
    out, n = redact_text(
        "GET http://api.internal:8080/v1/health 200", secrets=[], use_patterns=True
    )
    assert out == "GET http://api.internal:8080/v1/health 200"
    assert n == 0


def test_longest_secret_first_no_partial_clobber():
    # 同时给重叠的长短密钥,长的先替换,不被短的破坏。
    text = "key=ABCDEF-LONG-SECRET-7777 short=ABCDEF"
    out, n = redact_text(text, secrets=["ABCDEF", "ABCDEF-LONG-SECRET-7777"])
    assert "ABCDEF-LONG-SECRET-7777" not in out
    assert "ABCDEF" not in out


# ---- 复核补强:连接串边界 / 形态扩展 / JSON / 误伤收口 ----


def test_connstring_no_username_redis_form_redacted():
    # redis://:pass@(无用户名)是 redis-py/ioredis 默认凭据形态,必须脱密码。
    out, n = redact_text(
        "REDIS_URL=redis://:S3cretRedisPw1234@cache:6379/0", secrets=[], use_patterns=True
    )
    assert "S3cretRedisPw1234" not in out
    assert "cache:6379" in out  # host 保留
    assert n >= 1


def test_connstring_password_with_slash_fully_redacted():
    # 含 / 的密码不能只脱前半段或整条漏:密码段放行 /,在 @ 处收尾。
    out, n = redact_text(
        "dsn postgres://admin:Ab3xK/9zQ2w8L1m@prod-db:5432/main", secrets=[], use_patterns=True
    )
    assert "Ab3xK/9zQ2w8L1m" not in out
    assert "admin" in out  # 用户名保留
    assert "prod-db:5432" in out  # host 保留


def test_connstring_redaction_is_counted():
    # 连接串密码脱敏必须计数(此前裸 lambda 不计数)。
    _out, n = redact_text("postgres://u:p4ssw0rd@db:5432/app", secrets=[], use_patterns=True)
    assert n >= 1


def test_google_api_key_redacted():
    out, n = redact_text(
        "google error key=AIzaSyD1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P7", secrets=[], use_patterns=True
    )
    assert "AIzaSyD1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P7" not in out
    assert n >= 1


def test_github_fine_grained_pat_redacted():
    pat = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789ABCDEFGH"
    out, n = redact_text(f"clone with {pat} done", secrets=[], use_patterns=True)
    assert pat not in out
    assert n >= 1


def test_pem_private_key_block_redacted():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234abcdEFGHijklMNOP\n"
        "QRSTuvwx5678yzAB==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out, n = redact_text(f"leaked:\n{pem}\nend", secrets=[], use_patterns=True)
    assert "MIIEowIBAAKCAQEA1234abcdEFGHijklMNOP" not in out
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert n >= 1


def test_json_quoted_assignment_redacted():
    # 容器/Loki 常发 JSON 行:{"password": "value"} 的引号键也要脱值留键。
    out, n = redact_text('{"password": "s3cr3tValue"}', secrets=[], use_patterns=True)
    assert "s3cr3tValue" not in out
    assert "password" in out
    assert n >= 1


def test_credentials_keyword_with_value_redacted():
    out, n = redact_text("credentials=Ab3xZ9qPmK", secrets=[], use_patterns=True)
    assert "Ab3xZ9qPmK" not in out
    assert "credentials" in out


def test_bearer_english_word_not_over_redacted():
    # 保守:Bearer 后跟无数字的英文词(诊断信号)不当凭据脱掉。
    text = "invalid Bearer credentials provided"
    out, n = redact_text(text, secrets=[], use_patterns=True)
    assert out == text
    assert n == 0


def test_bearer_oauth_error_code_not_over_redacted():
    # WWW-Authenticate 的 error=invalid_token 是诊断要的信号,不脱。
    text = "WWW-Authenticate Bearer error=invalid_token"
    out, n = redact_text(text, secrets=[], use_patterns=True)
    assert "invalid_token" in out


def test_assign_colon_english_value_not_over_redacted():
    # 结构化/logfmt 日志 'keyword: english_reason' 不误脱(值无数字 → 当诊断文本保留)。
    text = "token: expired_signature returned 401"
    out, n = redact_text(text, secrets=[], use_patterns=True)
    assert "expired_signature" in out


def test_assign_value_special_char_before_digit_still_redacted():
    # digit 前瞻须扫完整值类:数字前有特殊字符(P@ss0rd 的 @)不能让真密码漏脱。
    out, n = redact_text("password=P@ssw0rd!", secrets=[], use_patterns=True)
    assert "P@ssw0rd" not in out
    assert "password" in out


def test_connstring_no_redos_on_long_unbroken_run():
    # ReDoS 守护:超长无空白的连接串状串(scheme://user:pass 反复、无闭合 @)是 _CONNSTR
    # 二次回溯的真正诱因——scheme run 逐起点贪婪扫 + password 吞到结尾再回溯找 @。三段收界
    # 前此输入 O(n²)(36k 约 4.7s);收界后线性(约几十 ms)。须远低于 1s,不阻塞事件循环。
    payload = ("a://:" + "p" * 40) * 800  # ~36k 字符,无 @
    t0 = time.perf_counter()
    out, _ = redact_text(payload, secrets=[], use_patterns=True)
    elapsed = time.perf_counter() - t0
    assert "@" not in out  # 无闭合 @ → 不该有任何连接串脱敏
    assert elapsed < 1.0, f"redact 过慢({elapsed:.2f}s)——_CONNSTR 二次回溯未收敛?"


def test_connstring_long_real_scheme_still_redacts_password():
    # scheme 收界后,真实长 scheme(postgresql=10 / mongodb+srv=11)的连接串仍正确脱密码。
    out, n = redact_text(
        "dsn postgresql://user:S3cretpass99@db:5432/x", secrets=[], use_patterns=True
    )
    assert "S3cretpass99" not in out
    assert "user" in out
    assert "db:5432" in out
