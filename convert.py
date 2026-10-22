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
