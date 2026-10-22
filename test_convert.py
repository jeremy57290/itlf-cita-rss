import importlib.util
import json
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
spec=importlib.util.spec_from_file_location('convert', Path(__file__).with_name('convert.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
NOW=datetime(2026,10,22,12,0,tzinfo=timezone.utc)

def ev(rid='x', kind='Accident', road='N7', restricted=1, created=None, revised=None, start=None):
    created=created or NOW-timedelta(minutes=2)
    revised=revised or created
    start=start or created
    e=dict(id=rid,kind=kind,road=road,direction='vers Luxembourg',place='',length='500 m',lane='voie de gauche',restricted=restricted,operational=1,closure=False,severity=[],comment='',start=m.iso(start),end='',created=m.iso(created),revised=m.iso(revised))
    e['fingerprint']=m.impact_signature(e)
    return e

class Test(unittest.TestCase):
    def test_sample(self):
        current, counts=m.parse(Path('/mnt/data/cita_sample.xml').read_bytes(),NOW)
        self.assertEqual(sum(counts.values()),20)
        self.assertEqual(len(current),20)
    def test_bootstrap_and_new(self):
        e=ev(); current={m.hashid(e['id']):e}
        state, stats=m.update(current,{},NOW)
        self.assertEqual(stats['new'],0)
        e2=ev('fresh',created=NOW+timedelta(minutes=2))
        nxt, stats=m.update({**current,m.hashid('fresh'):e2},state,NOW+timedelta(minutes=3))
        self.assertEqual(stats['new'],1)
        third,stats=m.update({**current,m.hashid('fresh'):e2},nxt,NOW+timedelta(minutes=4))
        self.assertEqual(stats['new'],0)
    def test_update_only_significant(self):
        e=ev(); key=m.hashid(e['id']);state,_=m.update({key:e},{},NOW)
        state['seen'][key]['announced']=True
        changed=dict(e,end=m.iso(NOW+timedelta(hours=3)),revised=m.iso(NOW+timedelta(minutes=3)))
        changed['fingerprint']=m.impact_signature(changed)
        self.assertEqual(changed['fingerprint'],e['fingerprint'])
        state2,stats=m.update({key:changed},state,NOW+timedelta(minutes=4))
        self.assertEqual(stats['updated'],0)
        changed['restricted']=2;changed['fingerprint']=m.impact_signature(changed)
        state3,stats=m.update({key:changed},state2,NOW+timedelta(minutes=5))
        self.assertEqual(stats['updated'],1)
    def test_end_confirmed(self):
        e=ev(); key=m.hashid('x'); another=ev('y')
        current={key:e,m.hashid('y'):another}
        state,_=m.update(current,{},NOW)
        state['seen'][key]['announced']=True
        for minutes in (5,10,15):
            state,stats=m.update({m.hashid('y'):another},state,NOW+timedelta(minutes=minutes))
        self.assertEqual(stats['ended'],1)
        self.assertTrue(any(i['category']=='ended' for i in state['items']))
    def test_planned_once(self):
        e=ev(kind='ConstructionWorks',start=NOW+timedelta(hours=3))
        key=m.hashid('x');state,_=m.update({}, {}, NOW)
        state,stats=m.update({key:e},state,NOW+timedelta(minutes=5))
        self.assertEqual(stats['planned'],1)
        state,stats=m.update({key:e},state,NOW+timedelta(minutes=10))
        self.assertEqual(stats['planned'],0)
    def test_summary_and_rss(self):
        e=ev(); key=m.hashid('x');state,stats=m.update({key:e},{},NOW)
        self.assertEqual(stats['summary'],1)
        xml=m.make_rss(state['items'],NOW)
        self.assertEqual(len(ET.fromstring(xml).findall('./channel/item')),1)
        self.assertIn('#CITA',xml)
        self.assertNotIn('Source : CITA',xml)
        state,stats=m.update({key:e},state,NOW+timedelta(minutes=5))
        self.assertEqual(stats['summary'],0)
    def test_recovery(self):
        old=ev('old');key=m.hashid('old');state,_=m.update({key:old},{},NOW)
        e=ev('missed',created=NOW+timedelta(minutes=15))
        state,stats=m.update({key:old,m.hashid('missed'):e},state,NOW+timedelta(minutes=50))
        self.assertEqual(stats['new'],1)
    def test_dst(self):
        a=datetime(2026,10,25,0,5,tzinfo=timezone.utc)
        b=datetime(2026,10,25,1,5,tzinfo=timezone.utc)
        self.assertNotEqual(m.summary_slot(a),m.summary_slot(b))
    def test_old_events_no_retroactive(self):
        e=ev(created=NOW-timedelta(days=10));key=m.hashid('x')
        state,_=m.update({}, {}, NOW)
        _,stats=m.update({key:e},state,NOW+timedelta(minutes=5))
        self.assertEqual(stats['new'],0)

if __name__=='__main__':unittest.main()
