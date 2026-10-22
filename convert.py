#!/usr/bin/env python3
"""CITA DATEX II -> RSS ITLF. Ne publie que des incidents nouveaux et récents."""
import argparse
import hashlib
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

SOURCE = 'https://cita.lu/info_trafic/datex/situationrecord36'
SITE = 'https://www.cita.lu/'
ROADS = {'A1', 'A3', 'A6', 'A13'}
XSI = '{http://www.w3.org/2001/XMLSchema-instance}type'
ALLOWED = {'Accident', 'VehicleObstruction', 'GeneralObstruction', 'AbnormalTraffic', 'TrafficCongestion'}
# Alerte créée il y a 20 minutes maximum; visibilité RSS 45 minutes maximum.
MAX_EVENT_AGE = timedelta(minutes=20)
RSS_LIFETIME = timedelta(minutes=45)

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
    results, counts = {}, {}
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
            road = next((r.upper() for r in values(record, 'roadName') if r.upper() in ROADS), '')
            if not road:
                continue
            start = date(first(record, 'overallStartTime'))
            end = date(first(record, 'overallEndTime'))
            if (start and start > now) or (end and end <= now):
                continue
            if first(record, 'validityStatus').lower() in ('suspended', 'cancelled', 'closed'):
                continue
            if first(record, 'probabilityOfOccurrence').lower() not in ('', 'certain'):
                continue
            rid = record.attrib.get('id')
            if not rid:
                continue
            if kind == 'VehicleObstruction':
                label = 'Véhicule en panne' if first(record, 'vehicleObstructionType') == 'brokenDownVehicle' else 'Obstacle / véhicule immobilisé'
            else:
                label = {'Accident': 'Accident signalé', 'TrafficCongestion': 'Congestion signalée',
                         'AbnormalTraffic': 'Circulation perturbée', 'GeneralObstruction': 'Obstacle sur la chaussée'}[kind]
            parts = [f'🚨 LUXEMBOURG – {road} | {label}']
            direction = first(record, 'roadDestination')
            if direction:
                parts.append('📍 Secteur / direction : ' + direction[:180])
            restricted = first(record, 'numberOfLanesRestricted')
            if restricted.isdigit() and int(restricted) > 0:
                parts.append(f'⚠️ {restricted} voie(s) restreinte(s) selon CITA.')
            parts.extend(['ℹ️ Vérifiez les conditions de circulation avant de partir.',
                          '📡 Source : CITA Luxembourg', '#ITLF #InfoTrafic #Luxembourg'])
            key = hashlib.sha256(rid.encode()).hexdigest()[:24]
            results[key] = {'title': parts[0], 'description': '\n'.join(parts),
                            'start': start.isoformat() if start else '', 'id': rid}
    return results, counts

def make_rss(items, now):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>',
             '<title>ITLF – Alertes trafic Luxembourg (CITA)</title>', f'<link>{SITE}</link>',
             '<description>Incidents routiers sélectionnés sur les autoroutes luxembourgeoises. Source : CITA.</description>',
             '<language>fr-LU</language>', f'<lastBuildDate>{format_datetime(now)}</lastBuildDate>']
    for item in items:
        pub = date(item.get('published')) or now
        lines.extend(['<item>', f'<title>{escape(item["title"])}</title>',
                      f'<description>{escape(item["description"])}</description>', f'<link>{SITE}</link>',
                      f'<guid isPermaLink="false">itlf-cita-{item["key"]}</guid>',
                      f'<pubDate>{format_datetime(pub)}</pubDate>', '</item>'])
    return '\n'.join(lines + ['</channel></rss>']) + '\n'

def update(current, state, now):
    seen = state.get('seen', {})
    if not isinstance(seen, dict):
        raise ValueError('state.json : seen doit être un objet')
    seen = dict(seen)
    initialized = state.get('safe_v2_initialized', False)
    if not initialized:
        for key in current:
            seen[key] = now.isoformat()
        new = []
        print('Initialisation prudente : aucun incident existant publié.')
    else:
        new = []
        for key, event in current.items():
            if key in seen:
                continue
            seen[key] = now.isoformat()  # Même un événement ancien ne sera pas republié plus tard.
            start = date(event.get('start'))
            if start is None or not (now - MAX_EVENT_AGE <= start <= now):
                print(f'Incident ignoré (début absent ou trop ancien) : {event["id"]}')
                continue
            new.append({'key': key, **event, 'published': now.isoformat()})
    # Les éléments de l'ancien RSS sans date de publication sont supprimés par prudence.
    history = state.get('items', [])
    if not isinstance(history, list):
        raise ValueError('state.json : items doit être une liste')
    active = set(current)
    retained = []
    for item in history:
        if not isinstance(item, dict) or item.get('key') not in active:
            continue
        published = date(item.get('published'))
        if published and now - RSS_LIFETIME <= published <= now:
            retained.append(item)
    retained = (new + retained)[:40]
    if len(seen) > 5000:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-5000:])
    return {'seen': seen, 'items': retained, 'safe_v2_initialized': True}, len(new)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', help='Fichier XML local : diagnostic seulement, sans modifier les fichiers')
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    if args.input:
        raw = Path(args.input).read_bytes()
    else:
        req = urllib.request.Request(SOURCE, headers={'User-Agent': 'ITLF-CITA-RSS/3.0', 'Accept': 'application/xml,text/xml'})
        with urllib.request.urlopen(req, timeout=35) as response:
            raw = response.read(10_000_001)
            if len(raw) > 10_000_000:
                raise ValueError('XML trop volumineux')
    current, counts = parse(raw, now)
    print('Types DATEX rencontrés : ' + json.dumps(counts, ensure_ascii=False, sort_keys=True))
    print(f'{len(current)} événements retenus après filtrage (accidents, obstacles, congestion).')
    if args.input:
        for event in current.values():
            print('DIAGNOSTIC UNIQUEMENT :', event['id'], event['title'])
        return
    state_path, rss_path = Path('docs/state.json'), Path('docs/rss.xml')
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'seen': {}, 'items': []}
    updated, added = update(current, state, now)
    rss = make_rss(updated['items'], now)
    # Préparer les deux contenus avant toute écriture.
    state_json = json.dumps(updated, ensure_ascii=False, indent=2) + '\n'
    state_path.parent.mkdir(parents=True, exist_ok=True)
    rss_path.write_text(rss, encoding='utf-8')
    state_path.write_text(state_json, encoding='utf-8')
    print(f'{added} nouvelles alertes ajoutées au RSS ; {len(updated["items"])} alertes actuellement visibles.')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERREUR :', exc, file=sys.stderr)
        sys.exit(1)
