#!/usr/bin/env python3
"""ITLF – conversion DATEX II vers RSS, alertes et bulletins de circulation.

Exécution GitHub Actions : python convert.py
Diagnostic hors réseau : python convert.py --input exemple.xml
Les données et les textes de publication sont distincts : aucune attribution
technique n'est insérée dans les publications, hormis le hashtag #CITA.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

SOURCE = 'https://cita.lu/info_trafic/datex/situationrecord36'
SITE = 'https://www.cita.lu/'
TZ = ZoneInfo('Europe/Luxembourg')
XSI = '{http://www.w3.org/2001/XMLSchema-instance}type'
HASHTAGS = '#ITLF #InfoTrafic #Luxembourg #CITA'
MAX_EVENT_AGE = timedelta(minutes=20)
RECOVERY_WINDOW = timedelta(hours=2)  # uniquement après interruption démontrée
RSS_LIFETIME = timedelta(minutes=45)
SUMMARY_HOURS = 2
SUMMARY_MAX_EVENTS = 20
PLANNED_HORIZON = timedelta(hours=24)
MAX_ITEMS = 100
MAX_SEEN = 15000
DISAPPEARANCE_CONFIRMATIONS = 3  # au moins trois relevés successifs
MIN_DISAPPEARANCE_AGE = timedelta(minutes=10)
MAX_END_NOTICE_AGE = timedelta(days=7)  # pas d'annonce de fin pour un chantier de plusieurs mois

LABELS = {
 'Accident': ('🚨', 'Accident'), 'VehicleObstruction': ('⚠️', 'Véhicule immobilisé'),
 'GeneralObstruction': ('⚠️', 'Obstacle sur la chaussée'),
 'AbnormalTraffic': ('🚗', 'Circulation perturbée'),
 'TrafficCongestion': ('🚗', 'Bouchon'),
 'MaintenanceWorks': ('🚧', 'Travaux de maintenance'),
 'ConstructionWorks': ('🚧', 'Travaux'),
 'EquipmentOrSystemFault': ('⚠️', 'Défaillance d’équipement routier'),
 'RoadOrCarriagewayOrLaneManagement': ('🔴', 'Restriction de circulation'),
 'EnvironmentalObstruction': ('⚠️', 'Obstacle environnemental'),
 'AnimalPresenceObstruction': ('⚠️', 'Animal sur la chaussée'),
 'NonWeatherRelatedRoadConditions': ('⚠️', 'État de chaussée perturbé'),
 'WeatherRelatedRoadConditions': ('🌧️', 'Conditions routières difficiles'),
 'PoorRoadInfrastructure': ('⚠️', 'Chaussée dégradée'),
 'SpeedManagement': ('🚗', 'Régulation de vitesse'),
 'PublicEvent': ('📅', 'Événement perturbant la circulation'),
 'ReroutingManagement': ('↪️', 'Déviation'),
 'GeneralNetworkManagement': ('🚗', 'Gestion de circulation'),
}
WORKS = {'MaintenanceWorks', 'ConstructionWorks'}
PLANNED = WORKS | {'PublicEvent', 'RoadOrCarriagewayOrLaneManagement'}
HIGH_PRIORITY = {'Accident', 'TrafficCongestion', 'AbnormalTraffic',
                 'VehicleObstruction', 'GeneralObstruction'}
CLOSURE_TERMS = ('roadclosed', 'carriagewayclosed', 'alllanesclosed', 'closed', 'closure')


def local(tag):
    return tag.rsplit('}', 1)[-1]


def nodes(element, name):
    return (e for e in element.iter() if local(e.tag) == name)


def vals(element, name):
    return [e.text.strip() for e in nodes(element, name) if e.text and e.text.strip()]


def first(element, name):
    return next(iter(vals(element, name)), '')


def date(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def iso(d):
    return d.isoformat() if d else ''


def hashid(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def tidy(text, limit=180):
    return re.sub(r'\s+', ' ', text or '').strip()[:limit]


def local_time(d):
    return d.astimezone(TZ).strftime('%d/%m/%Y à %Hh%M')


def public_comment(record):
    for block in nodes(record, 'generalPublicComment'):
        choices = [(v.attrib.get('lang', ''), tidy(v.text, 180))
                   for v in nodes(block, 'value') if v.text and v.text.strip()]
        if choices:
            return next((v for lang, v in choices if lang == 'fr'), choices[0][1])
    return ''


def location(record):
    # Ces champs sont réellement présents dans le DATEX transmis.
    road = tidy(first(record, 'roadName'), 90) or 'Route non précisée'
    direction = tidy(first(record, 'roadDestination'))
    place = tidy(first(record, 'locationDescription'))
    distance = first(record, 'lengthAffected')
    length = None
    try:
        metres = float(distance)
        if 50 <= metres < 100000:
            length = f'{metres / 1000:.1f} km'.replace('.', ',') if metres >= 1000 else f'{round(metres)} m'
    except (TypeError, ValueError):
        pass
    lanes = set(vals(record, 'laneUsage'))
    lane_labels = {'leftLane': 'voie de gauche', 'rightLane': 'voie de droite',
                   'middleLane': 'voie centrale', 'hardShoulder': "bande d’arrêt d’urgence"}
    lane_text = ', '.join(lane_labels[k] for k in sorted(lanes) if k in lane_labels)
    return road, direction, place, length, lane_text


def impact_signature(event):
    # Une date de fin, un numéro de version ou un commentaire cosmétique
    # ne suffisent PAS à provoquer une nouvelle publication.
    fields = ('kind', 'road', 'direction', 'restricted', 'operational',
              'lane', 'closure', 'severity', 'length')
    return hashid(json.dumps({k: event.get(k) for k in fields}, sort_keys=True, ensure_ascii=False))


def parse(raw, now):
    root = ET.fromstring(raw)
    if local(root.tag) not in {'payload', 'situationPublication'}:
        raise ValueError('Document DATEX inattendu : élément racine ' + local(root.tag))
    events, counts = {}, Counter()
    for situation in root.iter():
        if local(situation.tag) != 'situation':
            continue
        for record in situation:
            if local(record.tag) != 'situationRecord':
                continue
            kind = record.attrib.get(XSI, '').split(':')[-1]
            counts[kind] += 1
            if kind not in LABELS:
                continue
            rid = record.attrib.get('id')
            if not rid:
                continue
            start = date(first(record, 'overallStartTime'))
            end = date(first(record, 'overallEndTime'))
            created = date(first(record, 'situationRecordCreationTime'))
            revised = date(first(record, 'situationRecordVersionTime'))
            validity = first(record, 'validityStatus').lower()
            if validity in {'suspended', 'cancelled', 'closed', 'inactive', 'ended'}:
                continue
            if end and end <= now:
                continue
            if first(record, 'probabilityOfOccurrence').lower() not in {'', 'certain'}:
                continue
            if start and start > now + PLANNED_HORIZON:
                continue
            if start and start > now and kind not in PLANNED:
                continue
            road, direction, place, length, lane = location(record)
            def number(name):
                value = first(record, name)
                return int(value) if value.isdigit() else None
            management = sorted(set(vals(record, 'roadOrCarriagewayOrLaneManagementType') +
                                    vals(record, 'trafficConstrictionType') + vals(record, 'closureType')))
            closure = any(any(term in m.lower() for term in CLOSURE_TERMS) for m in management)
            event = {
                'id': rid, 'kind': kind, 'road': road, 'direction': direction,
                'place': place, 'length': length, 'lane': lane,
                'restricted': number('numberOfLanesRestricted'),
                'operational': number('numberOfOperationalLanes'),
                'closure': closure, 'severity': sorted(set(management)),
                'comment': public_comment(record),
                'start': iso(start), 'end': iso(end),
                'created': iso(created), 'revised': iso(revised),
            }
            event['fingerprint'] = impact_signature(event)
            events[hashid(rid)] = event  # compatible avec les identifiants des versions antérieures
    return events, dict(counts)


def event_label(e):
    emoji, label = LABELS[e['kind']]
    if e['kind'] == 'VehicleObstruction' and 'panne' in e.get('comment', '').lower():
        label = 'Véhicule en panne'
    if e.get('closure'):
        return '🔴', 'Fermeture / restriction de circulation'
    return emoji, label


def impact_lines(e):
    result = []
    if e.get('closure'):
        result.append('🔴 Fermeture signalée.')
    if e.get('restricted', 0) and e['restricted'] > 0:
        result.append(f"⚠️ {e['restricted']} voie(s) restreinte(s).")
    if e.get('lane'):
        result.append('🛣️ Voie concernée : ' + e['lane'] + '.')
    if e.get('length'):
        result.append('📏 Secteur concerné : environ ' + e['length'] + '.')
    return result


def describe(e, now, prefix=''):
    emoji, label = event_label(e)
    title = f'{emoji} {prefix}LUXEMBOURG – {e["road"]} | {label}'
    lines = [title]
    if e.get('direction'):
        lines.append('📍 ' + e['direction'])
    if e.get('place') and e['place'].lower() not in {'section courante'}:
        lines.append('📌 Secteur : ' + e['place'])
    lines += impact_lines(e)
    if e.get('comment'):
        lines.append('ℹ️ ' + e['comment'])
    start, end = date(e.get('start')), date(e.get('end'))
    if start and start > now:
        lines.append('🗓️ Début prévu : ' + local_time(start))
    if end:
        lines.append('🕒 Fin annoncée : ' + local_time(end))
    lines += ['', '⚠️ Restez prudents et adaptez votre itinéraire.', HASHTAGS]
    return title, '\n'.join(lines)


def entry(key, event_key, title, description, now, category):
    return {'key': key, 'event_key': event_key, 'title': title,
            'description': description, 'published': iso(now), 'category': category}


def summary_slot(now):
    local_now = now.astimezone(TZ)
    if local_now.hour % SUMMARY_HOURS:
        return None
    # Créneau identifié par l'heure UTC : les deux occurrences de 02h00 au
    # passage à l'heure d'hiver restent distinctes.
    slot = local_now.replace(minute=0, second=0, microsecond=0)
    return iso(slot.astimezone(timezone.utc))


def priority(e):
    return (0 if e.get('closure') else 1 if e['kind'] in HIGH_PRIORITY else
            2 if (e.get('restricted') or 0) > 0 else 3 if e['kind'] in WORKS else 4)


def make_summary(current, now, slot):
    active = [e for e in current.values() if not date(e.get('start')) or date(e['start']) <= now]
    if not active:
        return None
    active.sort(key=lambda e: (priority(e), e['road'], e.get('direction', '')))
    groups = {}
    for e in active:
        groups.setdefault(e['road'], []).append(e)
    title = '🚦 POINT TRAFIC LUXEMBOURG – ' + now.astimezone(TZ).strftime('%d/%m à %Hh%M')
    lines = [title, '', '🚗 Voici les principales perturbations de circulation :']
    shown = 0
    for road in sorted(groups, key=lambda r: (min(priority(e) for e in groups[r]), r)):
        if shown >= SUMMARY_MAX_EVENTS:
            break
        lines += ['', '🛣️ ' + road]
        for e in groups[road]:
            if shown >= SUMMARY_MAX_EVENTS:
                break
            _, label = event_label(e)
            detail = '• ' + label
            if e.get('direction'):
                detail += ' – ' + tidy(e['direction'], 75)
            if e.get('closure'):
                detail += ' (fermeture signalée)'
            elif e.get('restricted'):
                detail += f" ({e['restricted']} voie(s) restreinte(s))"
            lines.append(detail)
            shown += 1
    if len(active) > shown:
        lines.append(f'… et {len(active) - shown} autres perturbations en cours.')
    lines += ['', '⚠️ Certaines perturbations peuvent durer plusieurs jours. La circulation peut évoluer.',
              '🚗 Adaptez votre itinéraire et restez prudents.', HASHTAGS]
    return entry('summary-' + hashid(slot), '__summary__', title, '\n'.join(lines), now, 'summary')


def migrate_previous(previous, event, now):
    if isinstance(previous, dict):
        # Une ancienne signature incompatible ne déclenche aucune alerte.
        if previous.get('schema') == 5:
            return previous
        return {'schema': 5, 'fingerprint': event['fingerprint'],
                'event': event, 'last_seen': iso(now), 'missing': 0,
                'first_missing': '', 'announced': False, 'planned_announced': False}
    # Chaîne historique : événement déjà vu, mais sans informations comparables.
    return {'schema': 5, 'fingerprint': event['fingerprint'],
            'event': event, 'last_seen': iso(now), 'missing': 0,
            'first_missing': '', 'announced': False, 'planned_announced': False}


def update(current, state, now):
    seen = dict(state.get('seen', {}))
    if not isinstance(state.get('seen', {}), dict):
        raise ValueError('state.json : seen doit être un objet')
    history = state.get('items', [])
    if not isinstance(history, list):
        raise ValueError('state.json : items doit être une liste')
    bootstrap = not state.get('itlf_v5_initialized', False)
    last_success = date(state.get('last_success'))
    interrupted = bool(last_success and now - last_success > MAX_EVENT_AGE)
    window = RECOVERY_WINDOW if interrupted else MAX_EVENT_AGE
    stats = Counter({'new': 0, 'updated': 0, 'ended': 0, 'planned': 0, 'summary': 0})
    new_items = []
    for key, event in current.items():
        previous = seen.get(key)
        migrated = migrate_previous(previous, event, now) if previous is not None else None
        unseen = previous is None
        start, created, revised = (date(event.get(x)) for x in ('start', 'created', 'revised'))
        future = bool(start and start > now)
        recently_created = created or start
        recent_new = bool(recently_created and now - window <= recently_created <= now)
        recent_change = bool(revised and now - window <= revised <= now)
        # Un événement dont la création est ancienne ne devient pas nouveau
        # après un redémarrage. La première installation reste silencieuse.
        can_publish = not bootstrap and not future
        if unseen:
            if can_publish and recent_new:
                title, desc = describe(event, now)
                new_items.append(entry(key + '-n-' + event['fingerprint'], key, title, desc, now, 'new'))
                stats['new'] += 1
            record = {'schema': 5, 'fingerprint': event['fingerprint'], 'event': event,
                      'last_seen': iso(now), 'missing': 0, 'first_missing': '',
                      'announced': bool(can_publish and recent_new), 'planned_announced': False}
        else:
            record = migrated
            previous_event = record.get('event', {})
            changed = record.get('fingerprint') != event['fingerprint']
            # Pas d'annonce de mise à jour d'un événement jamais annoncé
            # individuellement : il figure déjà dans les bulletins récapitulatifs.
            if changed and can_publish and recent_change and record.get('announced'):
                title, desc = describe(event, now, prefix='MISE À JOUR – ')
                new_items.append(entry(key + '-u-' + event['fingerprint'] + '-' + hashid(event.get('revised', '')), key, title, desc, now, 'updated'))
                stats['updated'] += 1
            record.update({'schema': 5, 'fingerprint': event['fingerprint'], 'event': event,
                           'last_seen': iso(now), 'missing': 0, 'first_missing': ''})
        # Préavis : au plus une annonce par chantier, entre 24 h et 0 h avant.
        if (not bootstrap and future and event['kind'] in PLANNED and
                start <= now + PLANNED_HORIZON and not record.get('planned_announced')):
            title, desc = describe(event, now, prefix='À VENIR – ')
            new_items.append(entry(key + '-p-' + hashid(event['start']), key,
                                   title, desc, now, 'planned'))
            record['planned_announced'] = True
            record['announced'] = True
            stats['planned'] += 1
        # Lorsqu'un chantier annoncé commence, informer seulement si le
        # démarrage est confirmé par une création/modification récente.
        if (not bootstrap and not future and record.get('planned_announced') and
                not record.get('started_announced') and recent_change and
                start and now - MAX_EVENT_AGE <= start <= now):
            title, desc = describe(event, now, prefix='EN COURS – ')
            new_items.append(entry(key + '-start-' + hashid(event['start']), key,
                                   title, desc, now, 'new'))
            record['started_announced'] = True
            stats['new'] += 1
        seen[key] = record

    # Ne jamais annoncer une fin lors d'un relevé partiel/vide : la suppression
    # exige un jeu de données crédible et trois absences sur >=10 minutes.
    reliable = bool(current) and (not last_success or now - last_success <= timedelta(hours=4))
    for key, record in list(seen.items()):
        if key in current or not isinstance(record, dict) or record.get('schema') != 5:
            continue
        if not reliable:
            continue
        missing = int(record.get('missing', 0)) + 1
        record['missing'] = missing
        if missing == 1:
            record['first_missing'] = iso(now)
        first_missing = date(record.get('first_missing'))
        last_seen = date(record.get('last_seen'))
        previous_event = record.get('event') or {}
        start = date(previous_event.get('start'))
        end = date(previous_event.get('end'))
        recent_enough = bool(last_seen and now - last_seen <= MAX_END_NOTICE_AGE)
        # Fin annoncée uniquement si précédemment diffusée individuellement,
        # et non pour une perturbation planifiée qui n'avait pas commencé.
        if (not bootstrap and missing >= DISAPPEARANCE_CONFIRMATIONS and
            first_missing and now - first_missing >= MIN_DISAPPEARANCE_AGE and
            recent_enough and record.get('announced') and
            (not start or start <= now) and not record.get('ended_announced')):
            # Disparition confirmée ≠ preuve absolue de retour à la normale.
            title = f"✅ FIN DE SIGNALEMENT – {previous_event.get('road', 'Luxembourg')}"
            lines = [title, 'La perturbation précédemment signalée ne figure plus parmi les informations de circulation disponibles.']
            if previous_event.get('direction'):
                lines.append('📍 ' + previous_event['direction'])
            lines += ['⚠️ La reprise normale de la circulation reste à confirmer sur place.', HASHTAGS]
            new_items.append(entry(key + '-end-' + hashid(iso(now)), key,
                                   title, '\n'.join(lines), now, 'ended'))
            record['ended_announced'] = True
            stats['ended'] += 1
        seen[key] = record

    slot = summary_slot(now)
    last_slot = state.get('last_summary_slot')
    if slot and slot != last_slot and current:
        summary = make_summary(current, now, slot)
        if summary:
            new_items.append(summary)
            stats['summary'] += 1
            last_slot = slot

    # Les alertes de fin sont conservées pendant 45 minutes même si la
    # perturbation a disparu ; les autres événements disparus sont retirés.
    retained = []
    for item in history:
        if not isinstance(item, dict):
            continue
        published = date(item.get('published'))
        if not published or not now - RSS_LIFETIME <= published <= now:
            continue
        event_key = item.get('event_key') or item.get('key')
        if item.get('category') not in {'ended', 'summary'} and event_key not in current:
            continue
        retained.append(item)
    unique = {}
    for item in new_items + retained:
        unique.setdefault(item['key'], item)
    items = sorted(unique.values(), key=lambda e: e['published'], reverse=True)[:MAX_ITEMS]
    if len(seen) > MAX_SEEN:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1].get('last_seen', '')
                           if isinstance(kv[1], dict) else str(kv[1]))[-MAX_SEEN:])
    old_stats = state.get('stats', {})
    totals = dict(old_stats.get('totals', {})) if isinstance(old_stats, dict) else {}
    for category, count in stats.items():
        totals[category] = int(totals.get(category, 0)) + count
    totals['runs'] = int(totals.get('runs', 0)) + 1
    updated = {'seen': seen, 'items': items, 'safe_v2_initialized': True,
               'itlf_v3_initialized': True, 'itlf_v5_initialized': True,
               'last_summary_slot': last_slot, 'last_success': iso(now),
               'stats': {'last_run': iso(now), 'last_run_counts': dict(stats),
                         'totals': totals, 'active_events': len(current),
                         'visible_rss_items': len(items)}}
    return updated, dict(stats)


def make_rss(items, now):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>',
             '<title>ITLF – Informations trafic Luxembourg</title>',
             f'<link>{escape(SITE)}</link>',
             '<description>Alertes et points trafic sur les routes du Luxembourg.</description>',
             '<language>fr-LU</language>',
             f'<lastBuildDate>{format_datetime(now)}</lastBuildDate>']
    for item in items:
        pub = date(item.get('published')) or now
        parts.extend(['<item>', f'<title>{escape(item["title"])}</title>',
                      f'<description>{escape(item["description"])}</description>',
                      f'<link>{escape(SITE)}</link>',
                      f'<guid isPermaLink="false">itlf-cita-{escape(item["key"])}</guid>',
                      f'<pubDate>{format_datetime(pub)}</pubDate>', '</item>'])
    return '\n'.join(parts + ['</channel></rss>']) + '\n'


def download_xml():
    error = None
    for attempt in range(1, 5):
        try:
            request = urllib.request.Request(SOURCE, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; ITLF-CITA-RSS/5.0)',
                'Accept': 'application/xml,text/xml,*/*', 'Cache-Control': 'no-cache'})
            with urllib.request.urlopen(request, timeout=35) as response:
                raw = response.read(10_000_001)
                content_type = response.headers.get('Content-Type', '')
            if not raw.strip() or len(raw) > 10_000_000:
                raise ValueError('Réponse vide ou trop volumineuse')
            root = ET.fromstring(raw)
            if local(root.tag) not in {'payload', 'situationPublication'}:
                raise ValueError('Document XML inattendu : ' + local(root.tag))
            print(f'Téléchargement validé : {len(raw)} octets (tentative {attempt}).')
            return raw
        except (urllib.error.URLError, TimeoutError, ValueError, ET.ParseError) as exc:
            error = exc
            print(f'Tentative {attempt}/4 : {exc}', file=sys.stderr)
            if attempt < 4:
                time.sleep(3 * attempt)
    raise RuntimeError(f'Téléchargement impossible : {error}. Fichiers précédents préservés.')


def atomic_write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(contents, encoding='utf-8')
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', help='Fichier XML local : diagnostic sans écriture')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    raw = Path(args.input).read_bytes() if args.input else download_xml()
    current, counts = parse(raw, now)
    print('Types DATEX :', json.dumps(counts, ensure_ascii=False, sort_keys=True))
    print(f'{len(current)} perturbations reconnues.')
    if args.input:
        for event in current.values():
            print('DIAGNOSTIC :', event['id'], event['road'], event['kind'])
        return
    state_path, rss_path = Path('docs/state.json'), Path('docs/rss.xml')
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    # Un téléchargement vide inattendu ne doit pas provoquer des fins de
    # perturbation ou vider la publication précédente.
    if not current and state.get('seen'):
        raise RuntimeError('Aucune perturbation reconnue : vérification nécessaire, RSS conservé.')
    updated, counts = update(current, state, now)
    rss = make_rss(updated['items'], now)
    ET.fromstring(rss)
    # L'état est écrit d'abord pour éviter de réémettre des GUID si le script
    # s'arrête après l'écriture du RSS. Un déploiement GitHub reste transactionnel.
    atomic_write(state_path, json.dumps(updated, ensure_ascii=False, indent=2) + '\n')
    atomic_write(rss_path, rss)
    print('Publications créées :', json.dumps(counts, ensure_ascii=False))
    print(f"{len(updated['items'])} entrées RSS visibles. Statistiques dans docs/state.json.")


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERREUR :', exc, file=sys.stderr)
        sys.exit(1)
