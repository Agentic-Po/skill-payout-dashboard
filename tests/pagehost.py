"""Shared host for the execution-level page gates (loop 2, 2026-09-27).

Builds a page from a template exactly the way refresh.py does (minus the data,
which the harness injects), parses the page's own static markup into the id
tree tests/domshim.js uses, runs the page's scripts under
tests/render_harness.js (node, one shared vm realm per run), and extracts the
digit-bearing text the run rendered.

Used by test_render_exec.py, test_figure_parity.py and
test_render_mutation.py. No network, no dependencies beyond node.
"""
import collections
import html as htmllib
import json
import os
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHIM = os.path.join(HERE, "domshim.js")
HARNESS = os.path.join(HERE, "render_harness.js")

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr"}
TEXT_ATTRS = ("title", "data-tip", "data-tiphtml")


def have_node():
    return bool(shutil.which("node"))


def build_from_template(tpl_text):
    """refresh.py's render of index.html, with the /*__DATA__*/ slot left in."""
    csv = os.path.join(ROOT, "transfers_export.csv")
    mb = os.path.getsize(csv) / 1e6 if os.path.exists(csv) else 0.0
    import sys
    sys.path.insert(0, ROOT)
    import freshness
    tpl_text = tpl_text.replace("__STALE_MIN__", str(freshness.STALE_MINUTES))
    tpl = tpl_text.replace("__CSV_MB__", f"{mb:.0f} MB" if mb >= 1 else f"{mb:.1f} MB")
    return "<!doctype html>\n<html lang=\"en\">\n" + tpl + "\n</html>"


def scripts_of(page_html):
    return re.findall(r"<script>(.*?)</script>", page_html, re.S)


class _Tree(HTMLParser):
    """id tree ({id: {parent, tag, attrs}}) + static text/attr fragments."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # [(tag, id or None)]
        self.tree = {}
        self.texts = []          # static text nodes outside script/style
        self.attr_texts = []
        self._skip = 0

    def _nearest_id(self):
        for _, i in reversed(self.stack):
            if i:
                return i
        return None

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        for k in TEXT_ATTRS:
            if k in a and a[k]:
                self.attr_texts.append(a[k])
        i = a.get("id")
        if i:
            self.tree[i] = {"parent": self._nearest_id(), "tag": tag, "attrs": a}
        if tag in ("script", "style"):
            self._skip += 1
        if tag not in VOID:
            self.stack.append((tag, i))

    def handle_startendtag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        for k in TEXT_ATTRS:
            if k in a and a[k]:
                self.attr_texts.append(a[k])
        i = a.get("id")
        if i:
            self.tree[i] = {"parent": self._nearest_id(), "tag": tag, "attrs": a}

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        for j in range(len(self.stack) - 1, -1, -1):
            if self.stack[j][0] == tag:
                del self.stack[j:]
                break

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.texts.append(data)


def parse_static(page_html):
    body = re.sub(r"<script>.*?</script>", "<script></script>", page_html, flags=re.S)
    p = _Tree()
    p.feed(body)
    p.close()
    return p


def run(page_html, data_text, runs):
    """Run the page's scripts once per entry in `runs`; returns result dicts."""
    tree = parse_static(page_html).tree
    cfg = {"shim": SHIM, "tree": tree, "scripts": scripts_of(page_html),
           "data": data_text, "runs": runs}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(cfg, fh)
        path = fh.name
    try:
        r = subprocess.run(["node", "--max-old-space-size=4096", HARNESS, path],
                           capture_output=True, text=True, timeout=600)
    finally:
        os.unlink(path)
    if r.returncode != 0:
        raise RuntimeError("render harness crashed:\n" + (r.stderr or "")[-3000:])
    return json.loads(r.stdout)


# ---- digit-bearing text extraction (figure parity) ----
_MASKS = [
    (re.compile(r"\b\d+ (?:min|h|d) ago\b"), "<AGE>"),
    (re.compile(r"\bjust now\b"), "<AGE>"),
    (re.compile(r"Data is [^—]* old"), "Data is <AGE> old"),
    (re.compile(r"\b\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?Z?\b"), "<TS>"),
    (re.compile(r"\b\d\d:\d\d HKT\b"), "<HKT>"),
]


def mask(s):
    for rx, rep in _MASKS:
        s = rx.sub(rep, s)
    return s


class _Frag(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts, self.attrs = [], []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k in TEXT_ATTRS and v:
                self.attrs.append(v)

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        if data.strip():
            self.texts.append(data)


def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def digit_multiset(page_html, result):
    """Counter of (kind, masked text) for every static or script-written text
    node / tooltip attribute that contains a digit."""
    c = collections.Counter()
    st = parse_static(page_html)
    for t in st.texts:
        if re.search(r"\d", t):
            c[("static", mask(_norm(t)))] += 1
    for t in st.attr_texts:
        if re.search(r"\d", t):
            c[("static-attr", mask(_norm(htmllib.unescape(t))))] += 1
    for w in result["dump"]["writes"]:
        if "text" in w:
            if re.search(r"\d", w["text"]):
                c[("text", mask(_norm(w["text"])))] += 1
            continue
        f = _Frag()
        f.feed(w["html"])
        f.close()
        for t in f.texts:
            if re.search(r"\d", t):
                c[("text", mask(_norm(t)))] += 1
        for t in f.attrs:
            if re.search(r"\d", t):
                # tooltip html is itself markup — compare its text nodes
                g = _Frag()
                g.feed(t)
                g.close()
                for tt in g.texts or [t]:
                    if re.search(r"\d", tt):
                        c[("attr", mask(_norm(tt)))] += 1
    return c


def generated_ms(data_text):
    """scope.generated_iso as epoch ms (the pinned clock for deterministic runs)."""
    import datetime
    D = json.loads(data_text)
    sc = D.get("scope") or {}
    iso = sc.get("generated_iso") or (str(sc.get("generated", "")).replace(" ", "T") + ":00Z")
    dt = datetime.datetime.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
    return int(dt.timestamp() * 1000)
