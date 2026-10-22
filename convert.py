#!/usr/bin/env python3
"""Convertisseur prudent CITA DATEX II 3 -> RSS ITLF, bibliothèque standard."""
import argparse
import hashlib
import json
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
XSI = '{http://www.w3.org/2001/XMLSchema-instance}type'
ALLOWED = {'Accident', 'VehicleObstruction', 'GeneralObstruction', 'AbnormalTraffic', 'TrafficCongestion'}

def local(tag):
    return tag.rsplit('}', 1)[-1]

def values(element, name):
    return [n.text.strip() for n in element.iter() if local(n.tag) == name and n.text and n.text.strip()]

def first(element, name):
    return next(iter(values(element, name)), '')

def date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None

def parse(xml, now):
    root = ET.fromstring(xml)
    results = {}
    counts = {}
    for situation in root.iter():
        if local(situation.tag) != 'situation':
            continue
        for record in situation:
            if local(record.tag) != 'situationRecord':
                continue
            kind = record.attrib.get(XSI, '').split(':')[-1]
            counts[kind] = counts.get(kind, 0) + 1
            if kind not in ALLOWED:
                continue
            road = next((r for r in values(record, 'roadName') if r.upper() in ROADS), '')
            if not road:
                continue
            start, end = date(first(record, 'overallStartTime')), date(first(record, 'overallEndTime'))
            if (start and start > now) or (end and end <= now):
                continue
            if first(record, 'validityStatus').lower() == 'suspended':
                continue
            if first(record, 'probabilityOfOccurrence').lower() not in ('', 'certain'):
                continue
            if kind == 'VehicleObstruction':
                typ = first(record, 'vehicleObstructionType')
                label = 'Véhicule en panne' if typ == 'brokenDownVehicle' else 'Obstacle / véhicule immobilisé'
            elif kind == 'Accident':
                label = 'Accident signalé'
            elif kind == 'TrafficCongestion':
                label = 'Congestion signalée'
            elif kind == 'AbnormalTraffic':
                label = 'Circulation perturbée'
            else:
                label = 'Obstacle sur la chaussée'
            direction = first(record, 'roadDestination')
            restricted = first(record, 'numberOfLanesRestricted')
            parts = [f'🚨 LUXEMBOURG – {road} | {label}']
            if direction:
                parts.append('📍 Secteur / direction : ' + direction[:180])
            if restricted.isdigit() and int(restricted) > 0:
                parts.append(f'⚠️ {restricted} voie(s) restreinte(s) selon CITA.')
            parts += ['ℹ️ Vérifiez les conditions de circulation avant de partir.', '📡 Source : CITA Luxembourg', '#ITLF #InfoTrafic #Luxembourg']
            rid = record.attrib.get('id')
            if not rid:
                continue
            key = hashlib.sha256(rid.encode()).hexdigest()[:24]
            results[key] = {'title': parts[0], 'description': '\n'.join(parts), 'start': (start or now).isoformat(), 'id': rid}
    return results, counts

def make_rss(items, now):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>', '<title>ITLF – Alertes trafic Luxembourg (CITA)</title>', f'<link>{SITE}</link>', '<description>Incidents routiers sélectionnés sur les autoroutes luxembourgeoises. Source : CITA.</description>', '<language>fr-LU</language>', f'<lastBuildDate>{format_datetime(now)}</lastBuildDate>']
    for item in items:
        pub = date(item['start']) or now
        lines.extend(['<item>', f'<title>{escape(item["title"])}</title>', f'<description>{escape(item["description"])}</description>', f'<link>{SITE}</link>', f'<guid isPermaLink="false">itlf-cita-{item["key"]}</guid>', f'<pubDate>{format_datetime(pub)}</pubDate>', '</item>'])
    return '\n'.join(lines + ['</channel></rss>']) + '\n'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', help='XML DATEX local pour diagnostic sans modification des fichiers')
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    if args.input:
        raw = Path(args.input).read_bytes()
    else:
        req = urllib.request.Request(SOURCE, headers={'User-Agent': 'ITLF-CITA-RSS/2.0', 'Accept': 'application/xml,text/xml'})
        with urllib.request.urlopen(req, timeout=35) as response:
            raw = response.read(10_000_001)
            if len(raw) > 10_000_000:
                raise ValueError('XML trop volumineux')
    current, counts = parse(raw, now)
    print('Types DATEX rencontrés : ' + json.dumps(counts, ensure_ascii=False, sort_keys=True))
    print(f'{len(current)} événements retenus après filtrage (accidents, obstacles, congestion).')
    if args.input:
        for e in current.values():
            print('DIAGNOSTIC UNIQUEMENT :', e['id'], e['title'])
        return
    state_path, rss_path = Path('docs/state.json'), Path('docs/rss.xml')
    state_exists = state_path.exists()
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_exists else {'seen': {}, 'items': []}
    seen = state.get('seen', {})
    if not isinstance(seen, dict):
        raise ValueError('Format de state.json inattendu : seen doit être un objet')
    # Après un changement de filtre, initialiser les incidents existants sans les publier.
    # Seuls les incidents créés APRÈS cette exécution seront émis.
    if not state.get('safe_v2_initialized'):
        seen.update({k: now.isoformat() for k in current})
        new = []
        print('Initialisation prudente v2 : aucun incident existant ne sera publié.')
    else:
        new = [(k, e) for k, e in current.items() if k not in seen]
        seen.update({k: now.isoformat() for k, _ in new})
    if len(seen) > 5000:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-5000:])
    history = state.get('items', [])
    for key, e in new:
        history.insert(0, {'key': key, **e})
    history = history[:40]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if new or not rss_path.exists():
        rss_path.write_text(make_rss(history, now), encoding='utf-8')
    state_path.write_text(json.dumps({'seen': seen, 'items': history, 'safe_v2_initialized': True}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(new)} nouvelles alertes ajoutées au RSS.')

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('ERREUR :', e, file=sys.stderr)
        sys.exit(1)
