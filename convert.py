#!/usr/bin/env python3
"""ITLF : flux CITA DATEX II vers RSS, avec nouveautés et changements significatifs.

Utilisation:
    python convert.py                # Met à jour docs/rss.xml et docs/state.json
    python convert.py --input data.xml  # Diagnostic sans écriture
"""
import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

SOURCE = "https://cita.lu/info_trafic/datex/situationrecord36"
SITE = "https://www.cita.lu/"
XSI = "{http://www.w3.org/2001/XMLSchema-instance}type"
MAX_EVENT_AGE = timedelta(minutes=20)
RSS_LIFETIME = timedelta(minutes=45)
MAX_ITEMS = 100
MAX_SEEN = 15000
# Les travaux annoncés peuvent être publiés dans les 24 h avant leur début,
# uniquement si l'enregistrement CITA vient d'être créé ou modifié.
PLANNED_HORIZON = timedelta(hours=24)
ALLOWED = {
    "Accident", "VehicleObstruction", "GeneralObstruction", "AbnormalTraffic",
    "TrafficCongestion", "MaintenanceWorks", "ConstructionWorks",
    "EquipmentOrSystemFault", "RoadOrCarriagewayOrLaneManagement",
    "EnvironmentalObstruction", "AnimalPresenceObstruction", "NonWeatherRelatedRoadConditions",
    "WeatherRelatedRoadConditions", "PoorRoadInfrastructure", "SpeedManagement",
    "PublicEvent", "ReroutingManagement", "GeneralNetworkManagement",
}
WORKS = {"MaintenanceWorks", "ConstructionWorks"}
EQUIPMENT = {"EquipmentOrSystemFault"}
PLANNED = WORKS | {"PublicEvent", "RoadOrCarriagewayOrLaneManagement"}
LABELS = {
    "Accident": "Accident signalé",
    "VehicleObstruction": "Véhicule immobilisé",
    "GeneralObstruction": "Obstacle sur la chaussée",
    "AbnormalTraffic": "Circulation perturbée",
    "TrafficCongestion": "Bouchon / congestion",
    "MaintenanceWorks": "Travaux de maintenance",
    "ConstructionWorks": "Travaux",
    "EquipmentOrSystemFault": "Défaillance d'équipement routier",
    "RoadOrCarriagewayOrLaneManagement": "Restriction / gestion de circulation",
    "EnvironmentalObstruction": "Obstacle environnemental",
    "AnimalPresenceObstruction": "Animal sur la chaussée",
    "NonWeatherRelatedRoadConditions": "État de chaussée perturbé",
    "WeatherRelatedRoadConditions": "Conditions routières difficiles",
    "PoorRoadInfrastructure": "Infrastructure routière dégradée",
    "SpeedManagement": "Régulation de vitesse",
    "PublicEvent": "Événement perturbant la circulation",
    "ReroutingManagement": "Déviation de circulation",
    "GeneralNetworkManagement": "Gestion du réseau routier",
}

def local(tag):
    return tag.rsplit("}", 1)[-1]

def nodes(el, name):
    return [x for x in el.iter() if local(x.tag) == name]

def vals(el, name):
    return [x.text.strip() for x in nodes(el, name) if x.text and x.text.strip()]

def first(el, name):
    return next(iter(vals(el, name)), "")

def parse_date(raw):
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    except ValueError:
        return None

def fmt(d):
    return d.isoformat() if d else ""

def digest(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]

def get_road(record):
    # Pas de filtre autoroutier : routes nationales, autoroutes et autres axes.
    roads = vals(record, "roadName")
    return roads[0][:90] if roads else "Route non précisée"

def describe(record, kind, road, direction, restricted, start, end, status):
    label = LABELS.get(kind, kind)
    if kind == "VehicleObstruction" and first(record, "vehicleObstructionType") == "brokenDownVehicle":
        label = "Véhicule en panne"
    heading = f"🚨 LUXEMBOURG – {road} | {label}"
    parts = [heading]
    if direction:
        parts.append("📍 Direction / secteur : " + direction[:180])
    if restricted:
        parts.append(f"⚠️ {restricted} voie(s) restreinte(s) selon CITA.")
    # Ne pas inventer de localisation, de cause ni de fermeture.
    comment = ""
    for block in nodes(record, "generalPublicComment"):
        candidates = vals(block, "value")
        if candidates:
            comment = candidates[0]
            break
    if comment:
        parts.append("ℹ️ Précision CITA : " + re.sub(r"\s+", " ", comment)[:240])
    if start and start > datetime.now(timezone.utc):
        parts.append("🗓️ Début annoncé : " + start.astimezone(timezone(timedelta(hours=2))).strftime("%d/%m/%Y %H:%M") + " (heure indicative locale).")
    if end:
        parts.append("🕒 Fin annoncée : " + end.isoformat())
    parts.extend(["ℹ️ Prudence, vérifiez les conditions de circulation.",
                  "📡 Source : CITA Luxembourg", "#ITLF #InfoTrafic #Luxembourg"])
    return heading, "\n".join(parts)

def parse(raw, now):
    root = ET.fromstring(raw)
    counts, events = {}, {}
    for situation in root.iter():
        if local(situation.tag) != "situation":
            continue
        for record in situation:
            if local(record.tag) != "situationRecord":
                continue
            kind = record.attrib.get(XSI, "").split(":")[-1]
            counts[kind] = counts.get(kind, 0) + 1
            if kind not in ALLOWED:
                continue
            rid = record.attrib.get("id", "")
            if not rid:
                continue
            start = parse_date(first(record, "overallStartTime"))
            end = parse_date(first(record, "overallEndTime"))
            created = parse_date(first(record, "situationRecordCreationTime"))
            revised = parse_date(first(record, "situationRecordVersionTime"))
            status = first(record, "validityStatus").lower()
            if status in {"suspended", "cancelled", "closed", "inactive", "ended"}:
                continue
            if end and end <= now:
                continue
            probability = first(record, "probabilityOfOccurrence").lower()
            if probability not in {"", "certain"}:
                continue
            # Une annonce future est conservée uniquement si elle commence bientôt.
            if start and start > now + PLANNED_HORIZON:
                continue
            if start and start > now and kind not in PLANNED:
                continue
            restricted_raw = first(record, "numberOfLanesRestricted")
            restricted = int(restricted_raw) if restricted_raw.isdigit() else 0
            road = get_road(record)
            direction = first(record, "roadDestination")
            lane_info = sorted(set(vals(record, "laneUsage")))
            management = sorted(set(vals(record, "roadOrCarriagewayOrLaneManagementType") +
                                    vals(record, "trafficConstrictionType") +
                                    vals(record, "closureType")))
            # Pour les changements, utiliser des propriétés significatives,
            # pas la simple date de version CITA.
            signature = {
                "kind": kind, "road": road, "direction": direction,
                "restricted": restricted, "lanes": lane_info,
                "management": management, "end": fmt(end),
                "comment": vals(record, "value")[:3],
            }
            fingerprint = digest(json.dumps(signature, sort_keys=True, ensure_ascii=False))
            title, description = describe(record, kind, road, direction, restricted, start, end, status)
            key = digest(rid)  # Compatibilité avec les anciens identifiants mémorisés.
            events[key] = {
                "id": rid, "kind": kind, "title": title, "description": description,
                "start": fmt(start), "created": fmt(created), "revised": fmt(revised),
                "fingerprint": fingerprint, "road": road,
            }
    return events, counts

def make_rss(items, now):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0"><channel>',
             '<title>ITLF – Informations trafic Luxembourg (CITA)</title>',
             f'<link>{SITE}</link>',
             '<description>Alertes et évolutions du trafic sur les routes du Luxembourg. Source : CITA.</description>',
             '<language>fr-LU</language>',
             f'<lastBuildDate>{format_datetime(now)}</lastBuildDate>']
    for item in items:
        published = parse_date(item.get("published")) or now
        lines.extend(["<item>",
                      f'<title>{escape(item["title"])}</title>',
                      f'<description>{escape(item["description"])}</description>',
                      f'<link>{SITE}</link>',
                      f'<guid isPermaLink="false">itlf-cita-{escape(item["key"])}</guid>',
                      f'<pubDate>{format_datetime(published)}</pubDate>',
                      "</item>"])
    return "\n".join(lines + ["</channel></rss>"]) + "\n"

def update(current, state, now):
    seen = state.get("seen", {})
    if not isinstance(seen, dict):
        raise ValueError("state.json: seen doit être un objet")
    seen = dict(seen)
    old_history = state.get("items", [])
    if not isinstance(old_history, list):
        raise ValueError("state.json: items doit être une liste")
    active = set(current)
    # Migrer prudemment : ne pas republier tous les événements lors de l'installation.
    first_run = not state.get("itlf_v3_initialized", False)
    new_items = []
    for key, event in current.items():
        previous = seen.get(key)
        if isinstance(previous, dict):
            previous_fp = previous.get("fingerprint")
        else:
            previous_fp = None
        changed = isinstance(previous, dict) and previous_fp and previous_fp != event["fingerprint"]
        unseen = previous is None
        # L'heure de création doit être récente pour les nouveautés; pour les
        # évolutions, la date de modification CITA doit être récente.
        event_time = parse_date(event.get("created")) or parse_date(event.get("start"))
        change_time = parse_date(event.get("revised"))
        recent_new = event_time is not None and now - MAX_EVENT_AGE <= event_time <= now
        recent_change = change_time is not None and now - MAX_EVENT_AGE <= change_time <= now
        publish = (not first_run) and ((unseen and recent_new) or (changed and recent_change))
        # Si l'événement vient d'être connu mais a débuté il y a longtemps,
        # le critère de création récente suffit, pas de republication à l'infini.
        if publish:
            is_update = changed
            version_key = key + ("-u-" if is_update else "-n-") + event["fingerprint"]
            title = ("🔄 MISE À JOUR – " + event["title"]) if is_update else event["title"]
            description = ("🔄 Évolution signalée par CITA.\n" + event["description"]) if is_update else event["description"]
            new_items.append({"key": version_key, "event_key": key, "title": title,
                              "description": description, "published": now.isoformat()})
        # Garder la dernière signature; migrer les anciennes chaînes en dict.
        seen[key] = {"fingerprint": event["fingerprint"], "last_seen": now.isoformat()}
    retained = []
    for item in old_history:
        if not isinstance(item, dict):
            continue
        event_key = item.get("event_key") or item.get("key")
        if event_key not in active:
            continue
        published = parse_date(item.get("published"))
        if published and now - RSS_LIFETIME <= published <= now:
            retained.append(item)
    combined = new_items + retained
    # Déduplication stricte des GUID.
    unique = {}
    for item in combined:
        if item["key"] not in unique:
            unique[item["key"]] = item
    items = sorted(unique.values(), key=lambda x: x["published"], reverse=True)[:MAX_ITEMS]
    if len(seen) > MAX_SEEN:
        def last_seen(kv):
            v = kv[1]
            return v.get("last_seen", "") if isinstance(v, dict) else str(v)
        seen = dict(sorted(seen.items(), key=last_seen)[-MAX_SEEN:])
    return {"seen": seen, "items": items, "safe_v2_initialized": True,
            "itlf_v3_initialized": True}, len(new_items)

def download_xml():
    """Retente le téléchargement et rejette les réponses vides ou non XML."""
    last_error = None
    for attempt in range(1, 5):
        try:
            req = urllib.request.Request(SOURCE, headers={
                "User-Agent": "Mozilla/5.0 (compatible; ITLF-CITA-RSS/4.1)",
                "Accept": "application/xml,text/xml,*/*",
                "Cache-Control": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=35) as response:
                raw = response.read(10_000_001)
                content_type = response.headers.get("Content-Type", "")
                status = response.status
            if len(raw) > 10_000_000:
                raise ValueError("XML CITA trop volumineux")
            if not raw.strip():
                raise ValueError(f"Réponse CITA vide (HTTP {status}, Content-Type: {content_type})")
            if not raw.lstrip().startswith(b"<"):
                raise ValueError(f"Réponse CITA non XML (HTTP {status}, Content-Type: {content_type})")
            # Vérifier le XML avant toute écriture sur disque.
            ET.fromstring(raw)
            print(f"CITA : réponse XML valide, {len(raw)} octets (tentative {attempt}).")
            return raw
        except (urllib.error.URLError, TimeoutError, ValueError, ET.ParseError) as exc:
            last_error = exc
            print(f"CITA : tentative {attempt}/4 échouée : {exc}", file=sys.stderr)
            if attempt < 4:
                time.sleep(3 * attempt)
    raise RuntimeError(f"Source CITA indisponible après 4 tentatives : {last_error}. "
                       "Flux RSS et historique conservés sans modification.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="XML local : diagnostic uniquement, aucune écriture")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    if args.input:
        raw = Path(args.input).read_bytes()
    else:
        raw = download_xml()
    current, counts = parse(raw, now)
    print("Types DATEX rencontrés :", json.dumps(counts, ensure_ascii=False, sort_keys=True))
    print(f"{len(current)} événements routiers retenus (toutes routes, toutes catégories reconnues).")
    if args.input:
        for event in current.values():
            print("DIAGNOSTIC :", event["id"], event["title"])
        return
    state_path, rss_path = Path("docs/state.json"), Path("docs/rss.xml")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    updated, added = update(current, state, now)
    rss = make_rss(updated["items"], now)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    rss_path.write_text(rss, encoding="utf-8")
    state_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{added} nouvelles alertes / évolutions ; {len(updated['items'])} éléments RSS visibles.")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERREUR :", exc, file=sys.stderr)
        sys.exit(1)
