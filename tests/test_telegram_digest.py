"""Regression for overlength daily/weekly Telegram reports."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telegram_digest import digest_parts, units

short = "<b>Summary</b>\nEverything intact 🗓"
failed_size = "\n".join(["x" * 100] * 40 + ["x" * 31])
assert len(failed_size) == 4071
assert len(digest_parts(failed_size)) == 2
assert digest_parts(short) == [short]
for report in ["\n".join(["<b>Weekly evidence</b> " + "x" * 160] * 24),
               "\n".join(["<i>Warning and caveat</i> " + "🗓" * 150] * 45)]:
    parts = digest_parts(report)
    assert len(parts) > 1
    assert all(units(p) <= 3900 for p in parts)
    assert "\n".join(p.split("\n", 1)[1] for p in parts) == report
    for i, part in enumerate(parts, 1):
        assert f"part {i}/{len(parts)}" in part
        assert part.count("<b>") == part.count("</b>")
        assert part.count("<i>") == part.count("</i>")
try:
    digest_parts("x" * 5000)
except ValueError:
    pass
else:
    raise AssertionError("must not silently truncate an oversized line")
print("Digest size, emoji, numbering, complete evidence and HTML boundaries passed")

# Exercise the actual transport block: a partial delivery must never consume
# warning markers or mark the report successful.
import json, urllib.request, urllib.parse
from unittest.mock import patch
from types import SimpleNamespace
source = (Path(__file__).resolve().parents[1] / "notify.py").read_text()
start = source.index("# Only a fully delivered report")
end = source.index("# stamp AFTER", start)
code = compile(source[start:end], "digest_transport", "exec")
for fail_at in (None, 1):
    sent, outcomes = [], []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"ok":true}'
    def send(request, timeout):
        if len(sent) == fail_at:
            raise RuntimeError("synthetic_failure")
        sent.append(urllib.parse.parse_qs(request.data.decode()))
        return Response()
    namespace = dict(digest_parts=digest_parts, msg=report, json=json,
                     urllib=__import__("urllib"), os=SimpleNamespace(environ={
                         "TELEGRAM_CHAT_ID":"synthetic", "TELEGRAM_BOT_TOKEN":"synthetic"}),
                     URL="https://example.com", now=None,
                     _state=SimpleNamespace(record_send=lambda *args: outcomes.append(args[1])))
    with patch("urllib.request.urlopen", send):
        try: exec(code, namespace)
        except RuntimeError:
            assert fail_at is not None
    assert outcomes == ([True] if fail_at is None else [False])
    assert all(p["parse_mode"] == ["HTML"] and "reply_markup" in p for p in sent)
    assert len(sent) == (len(digest_parts(report)) if fail_at is None else fail_at)
print("Actual transport full-success and partial-failure accounting passed")
