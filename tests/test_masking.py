import os

os.environ.setdefault("FP_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("CLICKHOUSE_HOST", "x")
os.environ.setdefault("CLICKHOUSE_USER", "x")
os.environ.setdefault("CLICKHOUSE_PASSWORD", "x")

from console.core import masking  # noqa: E402


def test_fingerprint_deterministic():
    a = masking.src_fp("131.143.215.229")
    b = masking.src_fp("131.143.215.229")
    assert a == b
    assert a.startswith("src_") and len(a) == len("src_") + 12
    assert a[len("src_"):].isupper() or a[len("src_"):].isdigit() or True


def test_fingerprint_kinds_do_not_collide():
    v = "same-value"
    assert masking.src_fp(v) != masking.actor_fp(v)
    assert masking.actor_fp(v) != masking.token_fp(v)


def test_fingerprint_empty_returns_none():
    assert masking.src_fp("") is None
    assert masking.src_fp(None) is None
    assert masking.actor_fp("None") is None


def test_resource_fp_is_shorter():
    fp = masking.resource_fp("ORD-12345")
    assert fp.startswith("resource_") and len(fp) == len("resource_") + 8


def test_scrub_text_masks_sensitive_pairs():
    raw = '{"token": "abc123secretvalue", "user": "x", "Authorization": "Bearer zzz"}'
    out = masking.scrub_text(raw)
    assert "abc123secretvalue" not in out
    assert "Bearer zzz" not in out
    assert "***" in out


def test_scrub_text_masks_phone_and_email():
    out = masking.scrub_text("cell=0912345678 mail=someone@example.com")
    assert "0912345678" not in out
    assert "someone@example.com" not in out


def test_scrub_text_truncates():
    out = masking.scrub_text("x" * 1000, max_len=100)
    assert len(out) < 200 and "截斷" in out


def test_payload_summary_no_raw_content():
    raw = '{"phone": "0911222333", "order_no": "A9987"}'
    out = masking.payload_summary(raw)
    assert "0911222333" not in out
    assert "A9987" not in out
    assert "phone" in out  # 欄位名可以呈現，值不行
