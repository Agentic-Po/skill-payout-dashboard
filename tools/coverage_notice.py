#!/usr/bin/env python3
"""Neutral, read-only saved checkpoint summary for the coverage failure notice."""
import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from public_scan import descriptor, scan_id, validate_store

MOCA = '0x2b11834ed1feaed4b4b3a86a6f571315e25a884d'
MENTE = '0x4cd9a847f39106e19a4e41aea8a232e915c82af5'
SOURCES = (
 ('Treasury OUT','0xbd956171f5b50936f0ad1c4db80c022bd2442519','from',[MOCA,MENTE]),
 ('Treasury IN','0xbd956171f5b50936f0ad1c4db80c022bd2442519','to',[MOCA,MENTE]),
 ('Cognition IN','0xd85096faec1ac03075667b4c1a1661f5623bf111','to',[MENTE]),
 ('Sink IN','0xf0961686bc71b8a1f42e7888bd8160e9b6240f40','to',[MENTE]),
 ('Sink OUT','0xf0961686bc71b8a1f42e7888bd8160e9b6240f40','from',[MENTE]),
 ('Coupon OUT','0xb15afc65532f8ec4d39db521ad7eb5b9e9ef5acf','from',[MOCA]),
 ('Coupon IN','0xb15afc65532f8ec4d39db521ad7eb5b9e9ef5acf','to',[MOCA]),
)
SGT = timezone(timedelta(hours=8))

def utc(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ',value):
        raise ValueError('invalid timestamp')
    return datetime.strptime(value,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

def date(value):
    return value.astimezone(SGT).strftime('%d %b %Y %H:%M SGT')

def checkpoint(value, now):
    try:
        if not isinstance(value,dict): return None
        if any(type(value.get(k)) is not int for k in ('start','through','target')): return None
        if value['through']<value['start'] or value['target']<value['start']: return None
        if not isinstance(value.get('hash'),str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}',value['hash']): return None
        stamp=utc(value['timestamp'])
        if stamp>now: return None
        return stamp
    except (ValueError,KeyError): return None

def summary(root, now):
    """Never infer chain coverage from financial build clocks or event rows."""
    root=Path(root); now=utc(now) if isinstance(now,str) else now
    if now.tzinfo is None: raise ValueError('explicit UTC time required')
    now=now.astimezone(timezone.utc)
    docs=[json.loads((root/name).read_text()) for name in ('data.json','coupon_data.json')]
    aliases={scan_id(descriptor(w,d,t)):name for name,w,d,t in SOURCES}
    published={}
    for doc in docs:
        pending=doc.get('scope',{}).get('source_coverage',{}).get('pending',[])
        if not isinstance(pending,list): continue
        for leg in pending:
            try:
                desc=leg['descriptor'];sid=scan_id(desc)
                if sid not in aliases or desc!=descriptor(desc['wallet'],desc['direction'],desc['tokens']): continue
                published[sid]=leg
            except (TypeError,KeyError,ValueError): continue
    store=root/'pending_scans'; manifests={}; store_valid=True
    try:
        validate_store(str(store))
        for sid in aliases:
            path=store/(sid+'.json')
            if path.exists(): manifests[sid]=json.loads(path.read_text())
    except Exception:
        # A malformed store supplies no usable checkpoint evidence.
        manifests={}; store_valid=False
    lines=['A checkpoint was saved, but the scan does not yet cover every required source.','Saved chain checkpoints · as of '+date(now)];stamps=[];all_complete=True
    for sid,name in aliases.items():
        p=published.get(sid,{})
        value=manifests.get(sid,p)
        stamp=checkpoint(value,now) if store_valid else None
        gap='prefix' in p or 'prefix' in value
        if stamp is None:
            reason='unavailable (checkpoint validation failed)' if not store_valid else ('unavailable (no canonical checkpoint)' if value else 'not started / no saved checkpoint')
            lines.append(name+': '+reason);all_complete=False;continue
        age=max(0,int((now-stamp).total_seconds()//60));lag=f'{age//60}h {age%60:02d}m behind'
        label='suffix proof; earlier gap' if gap else 'saved checkpoint'
        lines.append(f'{name}: {date(stamp)} · {lag} · {label}')
        stamps.append(stamp)
        all_complete &= not gap and value.get('complete') is True and value['through']>=value['target']
    if all_complete and len(stamps)==7 and all(d.get('scope',{}).get('complete') is True and d.get('scope',{}).get('source_coverage',{}).get('coverage_ok') is True for d in docs):
        lines.append('All required sources verified through: '+date(min(stamps)))
    for name,doc in zip(('Treasury','Coupon'),docs):
        try:
            status='incomplete' if doc.get('scope',{}).get('complete') is not True else 'complete'
            lines.append(name+' saved figures built: '+date(utc(doc['scope']['generated_iso']))+' ('+status+'; build time, not chain coverage)')
        except (ValueError,KeyError,TypeError):lines.append(name+' saved figures build time: unavailable')
    lines.append('Catch-up continues through scheduled runs. Updates are delayed, not real-time; a quiet detector is not an all-clear.')
    return '\n'.join(lines)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));parser.add_argument('--now');args=parser.parse_args()
    # One explicit clock shared by every lag calculation.
    now=args.now or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:print(summary(args.root,now))
    except Exception:print('Saved checkpoint details unavailable. Healthy reporting withheld.')
