#!/usr/bin/env python3
"""Figure-parity gate (council loop 2, 2026-09-27): a page change must not
move a single rendered figure unless it means to.

Renders the page from template.html at a BASE revision (default origin/main)
and from the working template.html, on the SAME data.json with the same
pinned clock (scope.generated_iso + 5 min), under tests/domshim.js — and
asserts the multiset of digit-bearing text is identical: every static text
node, every text node the scripts wrote, and every tooltip (title /
data-tip / data-tiphtml) text node that contains a digit. Relative ages
("N min ago", "Data is N hours old") and timestamps are masked.

No baseline file is committed — data.json changes four times an hour, so a
stored baseline would be stale by the next cron run. The comparison is
template-vs-template on today's data.

A change that is SUPPOSED to move figures (a new column, a reworded
number) states that in its PR and runs with FIGURE_PARITY_ALLOW=1, which
prints the diff and exits 0.

  python3 tests/test_figure_parity.py [BASE_REF]      (default origin/main)
"""
import os
import copy
import datetime
import json
import re
from collections import Counter
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pagehost as P  # noqa: E402


def base_template(ref):
    r = subprocess.run(["git", "-C", P.ROOT, "show", f"{ref}:template.html"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


# Reviewed display-only equivalents for the grant/purchase ambiguity correction.
# Exact phrases only: an altered amount or any unrelated text still fails parity.
DISPLAY_EQUIVALENTS = {
    "$10 credits (new-user grant or purchased pack)": "stripe $10",
    "Historical $3 credits mix new-user grants and internal top-ups; new-user top-ups are now $10. The $10 size overlaps purchased packs, so its purpose remains ambiguous without a recorded payment type.":
        "The $3 bucket mixes new-user credits and internal top-ups — the internal top-ups sheet is the authoritative split.",
    "may include $10 new-user grants and coupon-delivered credits": "may include coupon-delivered credits",
    "Pack-sized on-chain credit deliveries (including possible $10 grants)": "Stripe-sized on-chain outflow",
    "includes credit deliveries (“top-ups delivered”), which mix purchased packs and grants. New-user top-ups are now $10; a $10-sized transfer alone cannot distinguish a grant from a purchase.": "",
}


def display_canonical(multiset):
    out = Counter()
    for (kind, text), count in multiset.items():
        for new, old in DISPLAY_EQUIVALENTS.items():
            text = text.replace(new, old)
        if text.strip():
            out[(kind, text)] += count
    return out


FRESHNESS_PROBE="""(()=>{const e=globalThis.__domshim.elements;return {tiles:e.get('ftiles')?.innerHTML||'',note:e.get('dailyCoverage')?.textContent||'',hidden:e.get('dailyCoverage')?.hidden};})()"""

def reviewed_freshness(base_result,work_result,data,now):
    """Authenticate the exact reviewed writes before substituting baseline text."""
    old=base_result['probe']['tiles'];new=work_result['probe']['tiles']
    scope=data['scope'];own=scope.get('source_coverage',{}).get('treasury_complete',scope.get('complete') is not False)
    stamp=scope.get('generated_iso') or ((scope.get('generated','').replace(' ','T')+':00Z') if scope.get('generated') else '')
    saved=stamp.replace('T',' ').removesuffix('Z')+' UTC' if stamp else 'time unavailable'
    expected=old
    if 'saved as of ' not in old:
        cards=old.split('<div class="tile"')
        assert len(cards)==7,'headline shape changed'
        for i,card in enumerate(cards[1:],1):
            if '<div class="k">Wallet balance</div>' in card:
                card=card.replace("What this wallet holds right now, at today's token price.","Saved on-chain balance, valued at the saved financial snapshot’s token prices.")
                card=re.sub(r'(<div class="d">)(.*?)(</div>)',lambda m:m[1]+m[2]+' · saved as of '+saved+m[3],card,count=1)
            if not own and any('<div class="k">'+label+'</div>' in card for label in ('Outflow — 24h','Inflow — 24h')):
                card=re.sub(r'(<div class="v">).*?(</div>)',r'\1Unavailable\2',card,count=1)
                card=re.sub(r'(<div class="d">).*?(</div>)',r'\1Coverage incomplete — this is not a verified zero.\2',card,count=1)
            if own:
                card=card.replace('<div style="font-size:11px;color:var(--warn);margin-top:4px">⚠ as of last complete fetch</div>','')
            cards[i]=card
        expected='<div class="tile"'.join(cards)
    assert new==expected,'unreviewed headline write differs from exact expected freshness change'
    note=work_result['probe']['note'];old_note=base_result['probe']['note']
    if not own:
        recorded=str(data['facts'].get('range',{}).get('to','')).replace('T',' ').removesuffix('Z')
        days=sorted(x['d'] for x in data['facts'].get('daily',[]) if re.fullmatch(r'\d{4}-\d{2}-\d{2}',x['d']))
        today=datetime.datetime.fromtimestamp(now/1000,datetime.timezone.utc).date().isoformat()
        next_day=(datetime.date.fromisoformat(days[-1])+datetime.timedelta(days=1)).isoformat() if days else None
        missing=(next_day if next_day==today else next_day+' through '+today) if next_day and next_day<=today else today
        wanted='Recorded transfers through '+(recorded+' UTC' if recorded else 'an unavailable time')+'. '+missing+' UTC: coverage not verified; missing days are unknown, not zero. Existing rows remain saved history; recent totals may be incomplete.'
        assert note==wanted and work_result['probe']['hidden'] is False,'daily unknown note is not exact or visible'
    else:
        assert not note and work_result['probe']['hidden'] is True,'complete daily note should be hidden'
    result=copy.deepcopy(work_result)
    replaced=0
    for write in result['dump']['writes']:
        if write.get('html')==new:
            write['html']=old;replaced+=1
        if 'text' in write and write['text']==note and note!=old_note:
            write['text']=old_note
    assert replaced==1,'headline fragment not uniquely identified'
    return result


def assert_mutations_rejected(pages,data,now,res):
    # A mutation to an unrelated balance or daily total must still fail this gate.
    mutated=copy.deepcopy(res[1]);mutated['probe']['tiles']=mutated['probe']['tiles'].replace('<div class="v">','$999 MUTATED ',1)
    try:reviewed_freshness(res[0],mutated,data,now)
    except AssertionError:pass
    else:raise AssertionError('unrelated wallet balance mutation escaped parity')
    baseline=display_canonical(P.digit_multiset(pages[0],res[0]))
    altered=reviewed_freshness(res[0],res[1],data,now)
    candidate=next(w for w in altered['dump']['writes'] if 'html' in w and 'height:10px;background:' in w['html'])
    candidate['html']+=' <span>$999999 unrelated daily mutation</span>'
    assert display_canonical(P.digit_multiset(pages[1],altered))!=baseline,'unrelated daily figure escaped parity'


def main():
    if not P.have_node():
        if os.environ.get("CI"):
            print("FAIL: node not available in CI — figure parity cannot run")
            sys.exit(1)
        print("SKIP: node not available — figure parity not enforced this run")
        sys.exit(0)
    ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    base = base_template(ref)
    if base is None:
        msg = f"base ref {ref!r} not available (shallow checkout? run `git fetch origin main`)"
        if os.environ.get("CI"):
            print("FAIL:", msg)
            sys.exit(1)
        print("SKIP:", msg)
        sys.exit(0)
    work = open(os.path.join(P.ROOT, "template.html")).read()
    data = open(os.path.join(P.ROOT, "data.json")).read()
    now = P.generated_ms(data) + 5 * 60000
    pages = [P.build_from_template(base), P.build_from_template(work)]
    res = [P.run(pg, data, [{"now": now,"probe":FRESHNESS_PROBE}])[0] for pg in pages]
    for name, r in zip((ref, "working"), res):
        assert not r["uncaught"], f"{name} template raised on real data: {r['uncaught']}"
    for own,overall in ((True,True),(True,False),(False,False)):
        fixture=json.loads(data)
        fixture['scope']['complete']=overall
        fixture['scope'].setdefault('source_coverage',{})['treasury_complete']=own
        trials=[P.run(pg,json.dumps(fixture),[{"now":now,"probe":FRESHNESS_PROBE}])[0] for pg in pages]
        assert all(not r['uncaught'] and not r['renderErrors'] for r in trials),'freshness parity fixture degraded'
        normalized=reviewed_freshness(trials[0],trials[1],fixture,now)
        assert display_canonical(P.digit_multiset(pages[0],trials[0]))==display_canonical(P.digit_multiset(pages[1],normalized)),'freshness fixture moved unrelated figures'
        assert_mutations_rejected(pages,fixture,now,trials)
    assert_mutations_rejected(pages,json.loads(data),now,res)
    res[1]=reviewed_freshness(res[0],res[1],json.loads(data),now)
    a, b = (display_canonical(P.digit_multiset(pg, r)) for pg, r in zip(pages, res))
    only_a, only_b = a - b, b - a
    print(f"{ref}: {sum(a.values())} digit-bearing text nodes · working: {sum(b.values())}")
    if not only_a and not only_b:
        print("test_figure_parity: PASS (identical digit-bearing text)")
        return
    for (kind, t), n in sorted(only_a.items()):
        print(f"  - [{kind}] x{n}: {t[:220]}")
    for (kind, t), n in sorted(only_b.items()):
        print(f"  + [{kind}] x{n}: {t[:220]}")
    if os.environ.get("FIGURE_PARITY_ALLOW") == "1":
        print("test_figure_parity: DIFF ALLOWED (FIGURE_PARITY_ALLOW=1)")
        return
    print(f"test_figure_parity: FAIL ({len(only_a)} removed, {len(only_b)} added)")
    sys.exit(1)


if __name__ == "__main__":
    main()
