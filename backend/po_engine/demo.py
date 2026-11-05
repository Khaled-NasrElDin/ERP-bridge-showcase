import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, RLock
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter, sleep

from .store import Store, one, record, notify_pending
from .integration import poll, poll_parallel, run_forever
from .monitor import reconcile, freshness, status, page
from .writeback import Conflict, request, verify

MAPPING = {
    'po': {'id': 'po_id', 'rev': 'rev', 'number': 'number', 'supplier': 'supplier'},
    'line': {'id': 'line_id', 'rev': 'rev', 'parent': 'po_id', 'sku': 'sku',
             'qty': 'qty', 'price': 'price', 'eta': 'eta'},
}
CUSTOM = {
    'po': {'id': 'order', 'rev': 'version', 'number': 'reference', 'supplier': 'vendor'},
    'line': {'id': 'item', 'rev': 'version', 'parent': 'order', 'sku': 'product',
             'qty': 'amount', 'price': 'cost', 'eta': 'delivery'},
}


class ERP:
    """A simulated client with explicit ordered feed/snapshot/CAS/idempotency contract."""
    safe_write = True
    write_capabilities = {
        'atomic_version_check': True,
        'durable_idempotency': True,
        'receipt_lookup': True,
        'po_aggregate_cas': True,
    }

    def __init__(self, mapping=MAPPING):
        self.mapping = mapping
        self._lock = RLock()  # Simulates an atomic transaction in the ERP.
        self.rows, self.events, self.receipts = {}, [], {}
        self.sequence, self.calls = 0, 0
        self.offline, self.lose_reply, self.snapshot_limit = False, False, None
        self.snapshot_override = None

    def change(self, kind, data, emit=True):
        with self._lock:
            self.rows[kind, str(data[self.mapping[kind]['id']])] = deepcopy(data)
            if kind == 'line':
                header = self.rows.get(('po', str(data[self.mapping['line']['parent']])))
                if header:
                    header[self.mapping['po']['rev']] += 1  # Every child mutation advances PO-wide CAS.
                    if emit:
                        self.change('po', header)
            if emit:
                self.sequence += 1
                self.events.append({'seq': self.sequence, 'kind': kind, 'data': deepcopy(data),
                                    'changed_at': datetime.now(timezone.utc).isoformat()})

    def changes(self, after, limit):
        with self._lock:
            if self.offline:
                raise ConnectionError('ERP offline')
            return deepcopy([e for e in self.events if e['seq'] > after][:limit])

    def snapshot(self):
        with self._lock:
            if self.offline:
                raise ConnectionError('ERP offline')
            if self.snapshot_override is not None:
                return deepcopy(self.snapshot_override)
            return {'items': [{'kind': k, 'data': deepcopy(v)} for (k, _), v in self.rows.items()],
                    'watermark': self.sequence, 'complete': True}

    def write_eta(self, line_id, eta, rev, po_rev, op_id):
        with self._lock:  # Check, update, and receipt must commit as one source-side operation.
            if self.offline:
                raise ConnectionError('ERP offline')
            if op_id in self.receipts:
                return deepcopy(self.receipts[op_id])
            line = deepcopy(self.rows['line', str(line_id)])
            header = self.rows['po', str(line[self.mapping['line']['parent']])]
            if line[self.mapping['line']['rev']] != rev or header[self.mapping['po']['rev']] != po_rev:
                raise Conflict('Source revision changed')
            self.calls += 1
            line[self.mapping['line']['rev']] += 1
            line[self.mapping['line']['eta']] = eta
            self.change('line', line)
            result = {'id': str(line_id), 'eta': eta, 'base_rev': rev,
                      'po_rev': po_rev, 'rev': line[self.mapping['line']['rev']]}
            self.receipts[op_id] = result
            if self.lose_reply:
                self.lose_reply = False
                raise TimeoutError('Committed; response lost')
            return deepcopy(result)

    def lookup(self, op_id):
        with self._lock:
            if self.offline:
                raise ConnectionError('ERP offline')
            return deepcopy(self.receipts.get(op_id))


def passed(label, condition):
    assert condition, label
    print('PASS:', label)


def show_step(store, label, source='netsuite', op_id=None):
    """Print incident state and local PO data during the existing tests."""
    with store.tx() as db:
        issues = db.execute("SELECT code,scope,detail FROM issues WHERE tenant='acme' AND source=? ORDER BY code", (source,)).fetchall()
        lines = []
        for line_id in ('L1', 'L2'):
            item = record(db, 'acme', source, 'line', line_id)
            if item:
                lines.append(f"{line_id}: qty={json.loads(item['body'])['qty']} (rev {item['revision']})")
        op = one(db, 'SELECT state FROM operations WHERE op_id=?', (op_id,)) if op_id else None
    print(f'\n[Walkthrough] {label}')
    print('  Local data:', ', '.join(lines) or 'none')
    print(f'  Active incidents: {len(issues)}')
    for issue in issues:
        print(f"    {issue['code']} [{issue['scope']}]: {issue['detail']}")
    if op_id:
        print(f"  Write {op_id}: {op['state'] if op else 'not found'}")


def main():
    with TemporaryDirectory() as folder:
        store = Store(Path(folder) / 'integration.sqlite')
        erp, other = ERP(), ERP(CUSTOM)
        erp.change('po', {'po_id': 'P1', 'rev': 1, 'number': 'PO-001', 'supplier': 'ACME'})
        erp.change('line', {'line_id': 'L1', 'rev': 1, 'po_id': 'P1', 'sku': 'A',
                            'qty': 5, 'price': '10.00', 'eta': '2026-11-10'})
        erp.change('po', {'po_id': 'P2', 'rev': 1, 'number': 'PO-002', 'supplier': 'BETA'})
        erp.change('line', {'line_id': 'L2', 'rev': 1, 'po_id': 'P2', 'sku': 'B',
                            'qty': 4, 'price': '3.50', 'eta': '2026-11-11'})
        other.change('po', {'order': 'P1', 'version': 1, 'reference': 'B-1', 'vendor': 'BETA'})
        other.change('line', {'item': 'L1', 'version': 1, 'order': 'P1', 'product': 'C',
                              'amount': 8, 'cost': 4, 'delivery': '2026-11-10'})
        poll_parallel(store, [('acme','netsuite',erp,MAPPING), ('beta','custom',other,CUSTOM)])
        with store.tx() as db:
            a = record(db, 'acme', 'netsuite', 'po', 'P1')
            b = record(db, 'beta', 'custom', 'po', 'P1')
            passed('Scoped source identity and distinct local PKs', a['pk'] != b['pk'])
            passed('PO header and child synchronized', bool(record(db,'acme','netsuite','line','L1')))

        bad = dict(erp.rows['line','L1'], rev=2, qty=-5)
        erp.change('line', bad)
        erp.change('line', dict(erp.rows['line','L2'], rev=2, qty=6))
        poll(store, 'acme','netsuite',erp,MAPPING)
        with store.tx() as db:
            passed('Invalid PO1 line quarantined, PO2 progresses',
                   record(db,'acme','netsuite','line','L1')['revision'] == 1 and
                   record(db,'acme','netsuite','line','L2')['revision'] == 2)
        show_step(store, 'Invalid P1 quantity rejected; P2 remains synchronized')
        try:
            request(store, 'acme','netsuite',erp,'L1','2026-12-01',approved=True,op_id='blocked')
            raise AssertionError('Unsafe write permitted')
        except ValueError:
            print('PASS: Invalid PO write is blocked')
        for invalid_approval in (False, 1):
            try:
                request(store, 'acme','netsuite',erp,'L2','2026-12-01',approved=invalid_approval)
                raise AssertionError('Unapproved write permitted')
            except PermissionError:
                print('PASS: Explicit Boolean approval required')
        passed('Healthy PO write proceeds', request(store,'acme','netsuite',erp,'L2','2026-12-01',
                                                     approved=True,op_id='healthy') == 'confirmed')
        poll(store,'acme','netsuite',erp,MAPPING)

        # Critical: invalid PO1 record must not prevent PO2 drift recovery.
        erp.change('line', dict(erp.rows['line','L2'],rev=4,qty=9),emit=False)
        before = erp.rows['line','L2']['qty']
        show_step(store, f'Silent ERP change: source L2 qty={before}, local is outdated')
        reconcile(store,'acme','netsuite',erp,MAPPING)
        show_step(store, 'Snapshot reconciled L2 against the ERP')
        with store.tx() as db:
            passed('Bad snapshot line isolated: healthy PO2 drift repaired',
                   json.loads(record(db,'acme','netsuite','line','L2')['body'])['qty'] == str(before))

        erp.change('line', dict(bad, rev=3, qty=5))
        poll(store,'acme','netsuite',erp,MAPPING)
        with store.tx() as db:
            passed('Corrected record clears isolated error',
                   one(db,"SELECT 1 FROM issues WHERE tenant='acme' AND code='record:line:L1'") is None)
            passed('Stale revision does not overwrite new data',
                   record(db,'acme','netsuite','line','L1')['revision'] == 3)

        show_step(store, 'Corrected P1 record accepted; validation incident cleared')
        erp.lose_reply = True
        calls = erp.calls
        passed('Lost ERP response becomes unknown', request(store,'acme','netsuite',erp,'L1',
                '2026-12-02',approved=True,op_id='lost') == 'unknown')
        show_step(store, 'ERP committed the write but its reply was lost', op_id='lost')
        passed('Receipt lookup confirms exactly one remote operation',
               verify(store,'lost',erp) == 'confirmed' and erp.calls == calls + 1)
        show_step(store, 'Receipt lookup confirmed the remote write', op_id='lost')
        poll(store,'acme','netsuite',erp,MAPPING)
        show_step(store, 'Incoming ERP update settled the write', op_id='lost')
        with store.tx() as db:
            passed('Change feed settles acknowledged write',
                   one(db,"SELECT state FROM operations WHERE op_id='lost'")['state']=='settled')

        # A missing event is not repaired by an older snapshot.
        saved_events = deepcopy(erp.events)
        missing = {'seq': erp.sequence + 1, 'kind':'line',
                   'data':dict(erp.rows['line','L1'], rev=5),
                   'changed_at': datetime.now(timezone.utc).isoformat()}
        later = {'seq': missing['seq'] + 1, 'kind':'line',
                 'data':dict(erp.rows['line','L1'], rev=6),
                 'changed_at': datetime.now(timezone.utc).isoformat()}
        erp.events = saved_events + [later]
        erp.sequence = later['seq']
        poll(store,'acme','netsuite',erp,MAPPING)
        with store.tx() as db:
            passed('Missing feed sequence detected and checkpoint preserved',
                   one(db,"SELECT gap_target FROM cursors WHERE tenant='acme'")['gap_target']==later['seq'])
        show_step(store, 'Missing change-feed sequence detected')
        old_snapshot = erp.snapshot()
        old_snapshot['watermark'] = missing['seq'] - 1
        erp.snapshot_override = old_snapshot
        passed('Stale snapshot cannot clear observed gap',
               reconcile(store,'acme','netsuite',erp,MAPPING) == 0)
        with store.tx() as db:
            passed('Gap stays open until sufficiently current snapshot',
                   one(db,"SELECT 1 FROM issues WHERE code='feed' AND tenant='acme'") is not None)
        erp.snapshot_override = None
        erp.change('line', dict(erp.rows['line','L1'],rev=7,qty=7),emit=False)
        reconcile(store,'acme','netsuite',erp,MAPPING)
        with store.tx() as db:
            passed('Current authoritative snapshot repairs gap',
                   one(db,"SELECT 1 FROM issues WHERE code='feed' AND tenant='acme'") is None)

        show_step(store, 'Fresh ERP snapshot repaired the known sequence gap')
        erp.offline = True
        poll(store,'acme','netsuite',erp,MAPPING)
        show_step(store, 'ERP outage detected')
        passed('ERP outage reported', any(x['code']=='poll' for x in status(store,'acme',True)))
        erp.offline = False
        reconcile(store,'acme','netsuite',erp,MAPPING)
        show_step(store, 'ERP available again; outage incident resolved')
        passed('Outage resolved after source verification',
               not any(x['code']=='poll' for x in status(store,'acme',True)))
        passed('Stale polling is detected', freshness(store,'acme','netsuite',at=
               datetime.now(timezone.utc)+timedelta(minutes=3)) > 120)
        passed('User and technical incident descriptions differ',
               status(store,'acme') != status(store,'acme',True))
        passed('Polling recovery clears freshness incident', freshness(store,'acme','netsuite') < 120)

        alerts = []
        notify_pending(store, alerts.append)
        with store.tx() as db:
            passed('Open/resolved incident alerts delivered durably',
                   bool(alerts) and one(db,'SELECT COUNT(*) n FROM alerts WHERE delivered=0')['n']==0)
        print(f'\n[Walkthrough] Notification callback received {len(alerts)} incident events.')
        erp.change('line', dict(erp.rows['line','L1'], rev=8, eta='invalid-date'))
        poll(store,'acme','netsuite',erp,MAPPING)
        passed('Unresolved data error visible in platform projection',
               any(x['code']=='record:line:L1' for x in status(store,'acme',True)))
        show_step(store, 'Invalid ETA remains visible to the status dashboard')
        Path('user.html').write_text(page(store,'acme'), encoding='utf-8')
        Path('technical.html').write_text(page(store,'acme',True), encoding='utf-8')
        print('\nAll demonstration checks passed. user.html / technical.html generated.')


def advanced_checks():
    """Regression scenarios that previously caused false recovery or stuck writes."""
    with TemporaryDirectory() as folder:
        store = Store(Path(folder) / 'edge.sqlite')
        erp = ERP()
        erp.change('po', {'po_id': 'P1', 'rev': 1, 'number': 'A1', 'supplier': 'X'})
        erp.change('line', {'line_id': 'L1', 'rev': 1, 'po_id': 'P1',
                            'sku': 'A', 'qty': 5, 'price': 10, 'eta': '2026-12-01'})
        erp.change('line', {'line_id': 'L1B', 'rev': 1, 'po_id': 'P1', 'sku': 'C',
                            'qty': 2, 'price': 10, 'eta': '2026-12-01'})
        erp.change('po', {'po_id': 'P2', 'rev': 1, 'number': 'A2', 'supplier': 'Y'})
        erp.change('line', {'line_id': 'L2', 'rev': 1, 'po_id': 'P2',
                            'sku': 'B', 'qty': 7, 'price': 20, 'eta': '2026-12-01'})
        poll(store, 'acme', 'erp', erp, MAPPING)
        erp.write_capabilities = dict(erp.write_capabilities, receipt_lookup=False)
        try:
            request(store, 'acme', 'erp', erp, 'L1', '2026-12-02',
                    approved=True, op_id='unsafe-adapter')
        except ValueError:
            print('PASS: Unsafe ERP adapter disables automated write-back')
        else:
            raise AssertionError('Unsafe ERP was allowed to write')
        finally:
            del erp.write_capabilities
        erp.change('po', {'po_id': 'P3', 'rev': 1, 'number': 'A1', 'supplier': 'Z'})
        poll(store, 'acme', 'erp', erp, MAPPING)
        codes = {p['code'] for p in status(store, 'acme', True)}
        passed('Duplicate PO number blocks both affected headers',
               {'duplicate:P1', 'duplicate:P3'} <= codes)
        erp.change('po', {'po_id': 'P3', 'rev': 2, 'number': 'A3', 'supplier': 'Z'})
        erp.change('line', {'line_id': 'L3', 'rev': 1, 'po_id': 'P3',
                            'sku': 'C', 'qty': 2, 'price': 4, 'eta': '2026-12-01'})
        poll(store, 'acme', 'erp', erp, MAPPING)
        reconcile(store, 'acme', 'erp', erp, MAPPING)
        passed('Uniqueness conflict resolves only after authoritative verification',
               not any(p['code'].startswith('duplicate:') for p in status(store, 'acme', True)))
        original_write = erp.write_eta
        def competing_sibling(*args):
            erp.change('line', dict(erp.rows['line', 'L1B'], rev=2, qty=-1), emit=False)
            return original_write(*args)
        erp.write_eta = competing_sibling
        passed('Concurrent sibling change rejected by PO-wide CAS',
               request(store, 'acme', 'erp', erp, 'L1', '2026-12-03',
                       approved=True, op_id='conflict') == 'conflict' and erp.calls == 0)
        erp.write_eta = original_write
        erp.change('line', dict(erp.rows['line', 'L1B'], rev=3, qty=2))
        erp.change('line', dict(erp.rows['line', 'L1'], rev=2))
        poll(store, 'acme', 'erp', erp, MAPPING)
        passed('Fresh authoritative revision resolves obsolete conflict',
               not any(p['code'] == 'write:conflict' for p in status(store, 'acme', True)))
        erp.lose_reply = True
        passed('Another lost response stays unknown until verified',
               request(store, 'acme', 'erp', erp, 'L1', '2026-12-04',
                       approved=True, op_id='background') == 'unknown')
        class Once:
            def __init__(self): self.calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls > 1
            def wait(self, interval): pass
        sent = []
        run_forever(store, [('acme', 'erp', erp, MAPPING)], Once(), sent.append, interval=0)
        with store.tx() as db:
            passed('Scheduler verifies uncertain write without repeating operation',
                   one(db, "SELECT state FROM operations WHERE op_id='background'")['state'] == 'settled')
        old = erp.rows.pop(('line', 'L2'))
        reconcile(store, 'acme', 'erp', erp, MAPPING)
        show_step(store, 'Missing source line flagged; local record retained', source='erp')
        passed('Missing source record is detected, not silently deleted',
               any(p['code']=='missing:line:L2' for p in status(store, 'acme', True)))
        erp.change('line', dict(old, rev=2))
        poll(store, 'acme', 'erp', erp, MAPPING)
        reconcile(store, 'acme', 'erp', erp, MAPPING)
        show_step(store, 'Restored source line clears the incident', source='erp')
        passed('Missing-record incident clears after verified restoration',
               not any(p['code']=='missing:line:L2' for p in status(store, 'acme', True)))

        # Same source identity under two parents must quarantine BOTH POs.
        conflicting = erp.snapshot()
        duplicate = next(e for e in conflicting['items']
                         if e['kind'] == 'line' and e['data']['line_id'] == 'L1')
        duplicate = deepcopy(duplicate)
        duplicate['data']['po_id'] = 'P2'
        duplicate['data']['rev'] += 1
        duplicate['data']['qty'] = 999
        conflicting['items'].append(duplicate)
        with store.tx() as db:
            trusted_revision = record(db, 'acme', 'erp', 'line', 'L1')['revision']
        erp.snapshot_override = conflicting
        reconcile(store, 'acme', 'erp', erp, MAPPING)
        with store.tx() as db:
            from .store import blocked
            passed('Cross-parent duplicate line blocks both affected POs',
                   blocked(db, 'acme', 'erp', 'P1') and blocked(db, 'acme', 'erp', 'P2')
                   and not blocked(db, 'acme', 'erp', 'P3'))
            duplicate_scopes = {r['scope'] for r in db.execute(
                "SELECT scope FROM issues WHERE code LIKE 'snapshot-duplicate:%'")}
            passed('Duplicate identity incidents identify both affected POs',
                   {'po:P1', 'po:P2'} <= duplicate_scopes)
            passed('Ambiguous snapshot must not overwrite trusted line',
                   record(db, 'acme', 'erp', 'line', 'L1')['revision'] == trusted_revision)
        erp.snapshot_override = None
        reconcile(store, 'acme', 'erp', erp, MAPPING)
        passed('Verified clean snapshot clears duplicate identity incidents',
               not any(x['code'].startswith('snapshot-duplicate:')
                       for x in status(store, 'acme', True)))

    # Competing ERP clients race against the SAME aggregate revision.
    concurrent = ERP()
    concurrent.change('po', {'po_id':'C', 'rev':1, 'number':'C1', 'supplier':'X'})
    for line_id in ('A', 'B'):
        concurrent.change('line', {'line_id':line_id, 'po_id':'C', 'rev':1,
                       'sku':line_id, 'qty':1, 'price':10, 'eta':'2026-12-01'})
    base_po_rev = concurrent.rows['po','C']['rev']
    original_change = concurrent.change
    def slow_change(kind, data, emit=True):
        if kind == 'line':
            sleep(0.03)  # Force overlap if write_eta() lacks an atomic source lock.
        return original_change(kind, data, emit)
    concurrent.change = slow_change
    start = Barrier(2)
    def race(item):
        start.wait()
        try:
            concurrent.write_eta(item, '2026-12-02', 1, base_po_rev, 'r-' + item)
            return 'accepted'
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(race, ('A', 'B')))
    passed('Atomic source CAS: two simultaneous writes yield one winner',
           sorted(outcomes) == ['accepted', 'conflict'] and concurrent.calls == 1)


def benchmark():
    """Synthetic load check: not a production latency/SLA guarantee."""
    class DelayedERP(ERP):
        def changes(self, after, limit):
            sleep(0.01)  # Simulated upstream API response delay.
            return super().changes(after, limit)

    with TemporaryDirectory() as folder:
        store = Store(Path(folder) / 'load.sqlite')
        sources = []
        lines_per_tenant = 1000
        for tenant in ('load_a', 'load_b'):
            erp = DelayedERP()
            erp.change('po', {'po_id': 'PO1', 'rev': 1, 'number': 'N-1', 'supplier': 'S'})
            for i in range(lines_per_tenant):
                erp.change('line', {
                    'line_id': f'L{i}', 'rev': 1, 'po_id': 'PO1',
                    'sku': f'SKU{i}', 'qty': 1, 'price': '10.00',
                    'eta': '2026-12-01',
                })
            sources.append((tenant, 'simulated_erp', erp, MAPPING))

        first_change = min(datetime.fromisoformat(source.events[0]['changed_at'])
                           for _, _, source, _ in sources)
        start = perf_counter()
        poll_parallel(store, sources)
        duration = perf_counter() - start
        lag = (datetime.now(timezone.utc) - first_change).total_seconds()
        with store.tx() as db:
            count = one(db, "SELECT COUNT(*) AS n FROM records WHERE kind='line'")['n']
        passed('Simulated load: all records visible', count == 2 * lines_per_tenant)
        print(f'Synthetic load: {count} lines, {duration:.2f}s processing, '
              f'{count/duration:.0f} records/s, {lag:.2f}s full-batch source-to-visible lag')
        # Includes generation and mock latency, but not production network/ERP rate limits.



def final_regressions():
    """Envelope isolation and parent-only CAS recovery regressions."""
    with TemporaryDirectory() as folder:
        store, erp = Store(Path(folder) / 'edge.sqlite'), ERP()
        for po in ('P1', 'P2'):
            erp.change('po', {'po_id':po, 'rev':1, 'number':po, 'supplier':'S'})
            erp.change('line', {'line_id':'L'+po[1:], 'rev':1, 'po_id':po,
                      'sku':'A', 'qty':2, 'price':10, 'eta':'2026-12-01'})
        erp.change('line', {'line_id':'L1B', 'rev':1, 'po_id':'P1',
                  'sku':'B', 'qty':2, 'price':10, 'eta':'2026-12-01'})
        poll(store, 'acme', 'erp', erp, MAPPING)

        erp.sequence += 1
        erp.events.append({'seq': erp.sequence, 'kind':['malformed'], 'data':{},
                           'changed_at':datetime.now(timezone.utc).isoformat()})
        erp.change('line', dict(erp.rows['line','L2'], rev=2, qty=5))
        poll(store, 'acme', 'erp', erp, MAPPING)
        with store.tx() as db:
            passed('Malformed feed kind quarantined without halting healthy PO',
                   record(db,'acme','erp','line','L2')['revision'] == 2 and
                   one(db,"SELECT 1 FROM issues WHERE code LIKE 'unknown:%'") is not None)

        erp.change('line', dict(erp.rows['line','L2'], rev=3, qty=6), emit=False)
        invalid = erp.snapshot()
        invalid['items'].append({'kind':['malformed'], 'data':{}})
        erp.snapshot_override = invalid
        reconcile(store, 'acme', 'erp', erp, MAPPING)
        with store.tx() as db:
            passed('Malformed snapshot entry preserves independent PO recovery',
                   record(db,'acme','erp','line','L2')['revision'] == 3 and
                   one(db,"SELECT 1 FROM issues WHERE code LIKE 'unknown:snapshot:%'") is not None)
        erp.snapshot_override = None
        reconcile(store, 'acme', 'erp', erp, MAPPING)
        passed('Clean authoritative snapshot resolves unknown-scope event',
               not any(x['code'].startswith('unknown:') for x in status(store,'acme',True)))

        erp.change('line', dict(erp.rows['line','L1B'], rev=2, qty=3))
        poll(store, 'acme', 'erp', erp, MAPPING)
        original = erp.write_eta
        def competing_sibling(*args):
            erp.change('line', dict(erp.rows['line','L1B'], rev=3, qty=4))
            return original(*args)
        erp.write_eta = competing_sibling
        passed('Parent-only aggregate conflict safely rejects original write',
               request(store,'acme','erp',erp,'L1','2026-12-04',
                       approved=True,op_id='parentonly') == 'conflict')
        erp.write_eta = original
        poll(store, 'acme', 'erp', erp, MAPPING)
        with store.tx() as db:
            passed('Parent-only conflict remains open until full source verification',
                   record(db,'acme','erp','line','L1')['revision'] == 1 and
                   one(db,"SELECT 1 FROM issues WHERE code='write:parentonly'") is not None)
        reconcile(store, 'acme', 'erp', erp, MAPPING)
        with store.tx() as db:
            passed('Snapshot clears obsolete conflict but never replays rejected write',
                   one(db,"SELECT 1 FROM issues WHERE code='write:parentonly'") is None and
                   one(db,"SELECT state FROM operations WHERE op_id='parentonly'")['state'] == 'conflict')
        passed('A NEW approved request can write against refreshed PO revision',
               request(store,'acme','erp',erp,'L1','2026-12-04',
                       approved=True,op_id='after-refresh') == 'confirmed')

def feed_integrity_regressions():
    """Invalid sequence requires explicit recovery; repeated full pages must terminate."""
    with TemporaryDirectory() as folder:
        store, erp = Store(Path(folder) / 'integrity.sqlite'), ERP()
        erp.change('po', {'po_id': 'P', 'rev': 1, 'number': 'P-1', 'supplier': 'S'})
        erp.change('line', {'line_id': 'L', 'rev': 1, 'po_id': 'P',
                            'sku': 'A', 'qty': 2, 'price': 10, 'eta': '2026-12-01'})
        poll(store, 'acme', 'erp', erp, MAPPING)
        with store.tx() as db:
            checkpoint = one(db, "SELECT seq FROM cursors WHERE tenant='acme'")['seq']

        changes = erp.changes
        erp.changes = lambda after, limit: [{'seq': None, 'kind': 'line',
            'data': dict(erp.rows['line', 'L']),
            'changed_at': datetime.now(timezone.utc).isoformat()}]
        poll(store, 'acme', 'erp', erp, MAPPING)
        with store.tx() as db:
            cur = one(db, "SELECT seq,gap_target FROM cursors WHERE tenant='acme'")
            passed('Invalid sequence persists a durable unsequenced integrity marker',
                   cur['seq'] == checkpoint and cur['gap_target'] == -1)
        erp.changes = changes
        reconcile(store, 'acme', 'erp', erp, MAPPING)
        with store.tx() as db:
            passed('Snapshot at previous checkpoint cannot clear unsequenced incident',
                   one(db, "SELECT 1 FROM issues WHERE code='feed'") is not None and
                   one(db, "SELECT gap_target FROM cursors WHERE tenant='acme'")['gap_target'] == -1)
        erp.change('line', dict(erp.rows['line', 'L'], rev=2, qty=3))
        reconcile(store, 'acme', 'erp', erp, MAPPING)
        poll(store, 'acme', 'erp', erp, MAPPING)
        with store.tx() as db:
            passed('Even a newer snapshot cannot guess coverage of unsequenced event',
                   one(db, "SELECT 1 FROM issues WHERE code='feed'") is not None and
                   one(db, "SELECT gap_target FROM cursors WHERE tenant='acme'")['gap_target'] == -1 and
                   json.loads(record(db, 'acme', 'erp', 'line', 'L')['body'])['qty'] == '3')
        try:
            request(store, 'acme', 'erp', erp, 'L', '2026-12-05',
                    approved=True, op_id='unsequenced-block')
        except ValueError:
            print('PASS: Unverified feed integrity blocks all write-back')
        else:
            raise AssertionError('Unsafe write allowed after unsequenced event')

    with TemporaryDirectory() as folder:
        store, erp = Store(Path(folder) / 'pagination.sqlite'), ERP()
        erp.change('po', {'po_id': 'P', 'rev': 1, 'number': 'P-1', 'supplier': 'S'})
        erp.change('line', {'line_id': 'L', 'rev': 1, 'po_id': 'P',
                            'sku': 'A', 'qty': 2, 'price': 10, 'eta': '2026-12-01'})
        poll(store, 'acme', 'erp', erp, MAPPING)
        with store.tx() as db:
            checkpoint = one(db, "SELECT seq FROM cursors WHERE tenant='acme'")['seq']
        changes, calls = erp.changes, []
        def repeated_page(after, limit):
            calls.append(after)
            if len(calls) > 3:
                raise RuntimeError('Unbounded pagination loop')
            return [deepcopy(erp.events[0])] * limit
        erp.changes = repeated_page
        poll(store, 'acme', 'erp', erp, MAPPING, limit=2)
        with store.tx() as db:
            passed('Repeated full page stops after one request and raises incident',
                   len(calls) == 1 and
                   one(db, "SELECT seq FROM cursors WHERE tenant='acme'")['seq'] == checkpoint and
                   one(db, "SELECT 1 FROM issues WHERE code='pagination'") is not None)
        erp.changes = changes
        reconcile(store, 'acme', 'erp', erp, MAPPING)
        passed('A clean authoritative snapshot resolves the pagination incident',
               not any(x['code'] == 'pagination' for x in status(store, 'acme', True)))


if __name__ == '__main__':
    main()
    advanced_checks()
    final_regressions()
    feed_integrity_regressions()
    benchmark()
