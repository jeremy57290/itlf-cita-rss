#!/usr/bin/env python3
"""CITA DATEX II 3.6 -> conservative RSS feed for ITLF. Stdlib only."""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

SOURCE = 'https://cita.lu/info_trafic/datex/situationrecord36'
SITE = 'https://www.cita.lu/'
ROADS = {'A1', 'A3', 'A6', 'A13'}
CATEGORIES = ('accident', 'obstruction', 'abnormaltraffic', 'trafficcongestion', 'networkmanagement', 'roadworks')
SIGNALS = ('accident', 'collision', 'obstruction', 'blocked', 'closure', 'closed', 'congestion', 'queue', 'stationarytraffic', 'slowtraffic', 'trafficjam', 'laneclosure', 'carriagewayclosure', 'roadclosure')


def local(tag):
    return tag.rsplit('}', 1)[-1].lower()


def texts(element, names):
    wanted = {n.lower() for n in names}
    return [n.text.strip() for n in element.iter() if local(n.tag) in wanted and n.text and n.text.strip()]


def first(element, names):
    values = texts(element, names)
    return values[0] if values else ''


def parse_time(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    except ValueError:
        return None


def clean(s):
    return re.sub(r'\s+', ' ', s).strip()


def events(xml_data, now):
    root = ET.fromstring(xml_data)
    result = {}
    for record in root.iter():
        name = local(record.tag)
        if name not in CATEGORIES and not name.endswith('situationrecord'):
            continue
        # Select concrete records only, not wrapper elements with nested records.
        if any(local(child.tag) in CATEGORIES for child in record):
            continue
        road_strings = texts(record, ('roadName', 'roadNumber', 'roadIdentifier'))
        road_text = ' '.join(road_strings).upper()
        matches = re.findall(r'(?<![A-Z0-9])A\s*(?:1|3|6|13)(?!\d)', road_text)
        roads = sorted({re.sub(r'\s+', '', x) for x in matches})
        if not roads:
            continue  # Fail closed: no inferred motorway from unrelated free text.
        tags = ' '.join(local(x.tag) for x in record.iter())
        vals = ' '.join(texts(record, ('accidentType', 'obstructionType', 'trafficStatus', 'roadOrCarriagewayOrLaneManagementType', 'roadworksType', 'comment', 'value'))).lower()
        kind = name.lower()
        signal = any(s in (tags + ' ' + vals).replace('_', '').replace(' ', '') for s in SIGNALS)
        if not signal and kind not in ('accident', 'obstruction', 'abnormaltraffic', 'trafficcongestion'):
            continue
        start = parse_time(first(record, ('overallStartTime',)))
        end = parse_time(first(record, ('overallEndTime',)))
        if start and start > now:
            continue
        if end and end <= now:
            continue
        status = first(record, ('validityStatus',)).lower()
        if status in ('suspended', 'definedbyvaliditytimeperiod'):
            continue
        rid = record.attrib.get('id', '')
        if not rid:
            continue
        comment = first(record, ('comment', 'value', 'locationDescription'))
        loc = first(record, ('locationDescription', 'roadDestination', 'directionBoundOnLinearSection'))
        label = 'Accident' if 'accident' in (kind + tags) else 'Fermeture / restriction' if any(x in (tags + vals).lower() for x in ('closure', 'closed', 'blocked')) else 'Perturbation routière'
        title = f'🚨 LUXEMBOURG – {" / ".join(roads)} | {label}'
        details = clean(comment)[:400]
        place = clean(loc)[:180]
        desc = f'{title}\n' + (f'📍 {place}\n' if place else '') + (f'ℹ️ {details}\n' if details else '') + '⚠️ Adaptez votre conduite et vérifiez les conditions de circulation.\n📡 Source : CITA Luxembourg\n#ITLF #InfoTrafic #Luxembourg'
        key = hashlib.sha256(rid.encode()).hexdigest()[:24]
        result[key] = {'title': title, 'description': desc, 'start': (start or now).isoformat(), 'id': rid}
    return result


def rss(items, now):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>', '<title>ITLF – Alertes trafic Luxembourg (CITA)</title>', f'<link>{SITE}</link>', '<description>Événements routiers CITA sélectionnés pour ITLF. Source officielle : CITA Luxembourg.</description>', '<language>fr-LU</language>', f'<lastBuildDate>{format_datetime(now)}</lastBuildDate>']
    for key, e in items:
        pub = parse_time(e['start']) or now
        parts.extend(['<item>', f'<title>{escape(e["title"])}</title>', f'<description>{escape(e["description"])}</description>', f'<link>{SITE}</link>', f'<guid isPermaLink="false">itlf-cita-{key}</guid>', f'<pubDate>{format_datetime(pub)}</pubDate>', '</item>'])
    parts.append('</channel></rss>')
    return '\n'.join(parts) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', help='Local DATEX XML file for testing')
    parser.add_argument('--bootstrap-publish', action='store_true', help='DANGEROUS: publish current incidents on first run')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    state_path = Path('docs/state.json')
    rss_path = Path('docs/rss.xml')
    if args.input:
        data = Path(args.input).read_bytes()
    else:
        req = urllib.request.Request(SOURCE, headers={'User-Agent': 'ITLF-CITA-RSS/1.0 (GitHub Actions)', 'Accept': 'application/xml,text/xml'})
        with urllib.request.urlopen(req, timeout=35) as response:
            data = response.read(10_000_001)
            if len(data) > 10_000_000:
                raise ValueError('Source XML too large')
    current = events(data, now)
    state_exists = state_path.exists()
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_exists else {'seen': {}, 'items': []}
    seen = state.get('seen', {})
    if not state_exists and not args.bootstrap_publish:
        seen.update({k: now.isoformat() for k in current})
        new = []
        print(f'Initialisation : {len(current)} événements mémorisés, aucune publication initiale.')
    else:
        new = [(k, v) for k, v in current.items() if k not in seen]
        for k, _ in new:
            seen[k] = now.isoformat()
    # Retain history to prevent repeat publication of a reappearing ID.
    # Cap state size, oldest first, at 5000 entries.
    if len(seen) > 5000:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-5000:])
    history = state.get('items', [])
    for k, v in new:
        history.insert(0, {'key': k, **v})
    history = history[:40]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Do not change RSS when there is no new incident: avoid unnecessary IFTTT polls.
    if new or not rss_path.exists():
        rss_path.write_text(rss([(x['key'], x) for x in history], now), encoding='utf-8')
    state_path.write_text(json.dumps({'seen': seen, 'items': history}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(current)} événements sélectionnés, {len(new)} nouveaux publiés dans le RSS.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ÉCHEC: {exc}', file=sys.stderr)
        sys.exit(1)
