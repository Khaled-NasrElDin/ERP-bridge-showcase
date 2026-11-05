import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from .store import one, record, flag, clear, now

SCHEMAS = {
    'po': ('id', 'rev', 'number', 'supplier'),
    'line': ('id', 'rev', 'parent', 'sku', 'qty', 'price', 'eta'),
}


def normalize(kind, raw, mapping):
    if not isinstance(kind, str) or kind not in SCHEMAS or not isinstance(raw, dict):
        raise ValueError('Unsupported kind or record shape')
    data = {field: raw[mapping[kind][field]] for field in SCHEMAS[kind]}
    if any(v is None or str(v).strip() == '' for v in data.values()):
        raise ValueError('Missing required value')
    data['id'] = str(data['id'])
    if type(data['rev']) is not int or data['rev'] < 1:
        raise ValueError('Invalid source revision')
    if kind == 'po':
        data['number'], data['supplier'] = str(data['number']), str(data['supplier'])
    else:
        data['parent'], data['sku'] = str(data['parent']), str(data['sku'])
        for field in ('qty', 'price'):
            try:
                val = Decimal(str(data[field]))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError('Invalid ' + field) from exc
            if not val.is_finite() or val < 0 or (field == 'qty' and val == 0):
                raise ValueError('Invalid ' + field)
            data[field] = str(val.normalize())
        data['eta'] = date.fromisoformat(str(data['eta'])).isoformat()
    return data


def apply(db, tenant, source, mapping, kind, raw, *, tag='event', error=None):
    valid_kind = isinstance(kind, str) and kind in SCHEMAS
    names = mapping.get(kind, {}) if valid_kind else {}
    sid = raw.get(names.get('id')) if isinstance(raw, dict) else None
    prior = record(db, tenant, source, kind, sid) if sid is not None else None
    claimed_parent = raw.get(names.get('parent')) if kind == 'line' and isinstance(raw, dict) else sid
    old_parent = prior['parent'] if prior else None
    scope = 'po:' + str(claimed_parent or old_parent) if claimed_parent or old_parent else '*'
    key = f'record:{kind}:{sid}' if valid_kind and sid is not None else f'unknown:{tag}'
    if old_parent and claimed_parent is not None and str(claimed_parent) != old_parent:
        flag(db, tenant, source, key + ':old-parent', 'po:' + old_parent,
             'Parent reassignment is not automatic; old PO needs review')
    try:
        if error:
            raise ValueError('Source record access/permission error')
        data = normalize(kind, raw, mapping)
        parent = data['parent'] if kind == 'line' else None
        if kind == 'line' and not record(db, tenant, source, 'po', parent):
            raise ValueError('Parent PO unavailable')
        if prior and prior['parent'] != parent:
            raise ValueError('Parent changed; explicit transfer/reconciliation required')
        body = json.dumps(data, sort_keys=True)
        if prior and data['rev'] <= prior['revision']:
            if data['rev'] == prior['revision'] and body != prior['body']:
                raise ValueError('Different content at same source revision')
            return False
        if kind == 'po':
            other = one(db, '''SELECT source_id FROM records WHERE tenant=? AND source=?
                        AND kind='po' AND po_number=? AND source_id<>?''',
                        (tenant, source, data['number'], data['id']))
            if other:
                flag(db, tenant, source, 'duplicate:' + other['source_id'],
                     'po:' + other['source_id'], 'Duplicate PO reference number')
                flag(db, tenant, source, 'duplicate:' + data['id'],
                     'po:' + data['id'], 'Duplicate PO reference number')
                raise ValueError('Duplicate purchase order number')
        db.execute('''INSERT INTO records(tenant,source,kind,source_id,parent,revision,po_number,body)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(tenant,source,kind,source_id)
                    DO UPDATE SET parent=excluded.parent,revision=excluded.revision,
                                  po_number=excluded.po_number,body=excluded.body''',
                   (tenant, source, kind, data['id'], parent, data['rev'],
                    data['number'] if kind == 'po' else None, body))
        clear(db, tenant, source, key)
        clear(db, tenant, source, 'drift:' + kind + ':' + data['id'])
        clear(db, tenant, source, key + ':old-parent')
        if kind == 'line':
            clear(db, tenant, source, 'empty:' + parent)
            for op in db.execute('''SELECT * FROM operations WHERE tenant=? AND source=?
                                  AND line_id=? AND state IN ('confirmed','conflict')''',
                                 (tenant, source, data['id'])).fetchall():
                if op['state'] == 'conflict':
                    if data['rev'] > op['base_rev']:
                        clear(db, tenant, source, 'write:' + op['op_id'])
                    continue
                if data['rev'] >= op['remote_rev']:
                    if data['rev'] == op['remote_rev']:
                        state = 'settled' if data['eta'] == op['eta'] else 'conflict'
                    else:
                        state = 'superseded'  # ERP advanced beyond the acknowledged revision
                    db.execute('UPDATE operations SET state=? WHERE op_id=?', (state, op['op_id']))
                    if state == 'conflict':
                        flag(db, tenant, source, 'write:' + op['op_id'], 'po:' + parent,
                             'ERP value differs at acknowledged source revision')
                    else:
                        clear(db, tenant, source, 'write:' + op['op_id'])
        return True
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        flag(db, tenant, source, key, scope, str(exc))
        return False


def poll(store, tenant, source, client, mapping, limit=100):
    # Per-page atomic processing and cursor; errors quarantine by parent PO.
    if type(limit) is not int or limit < 1:
        raise ValueError('Page size must be positive')
    try:
        while True:
            with store.tx() as db:
                cur = one(db, 'SELECT seq FROM cursors WHERE tenant=? AND source=?', (tenant, source))
                seq = cur['seq'] if cur else 0
            requested_seq = seq
            page = client.changes(seq, limit)
            if not isinstance(page, list):
                raise ValueError('Invalid change-feed response')
            gap = False
            with store.tx() as db:
                cur = one(db, 'SELECT seq,gap_target FROM cursors WHERE tenant=? AND source=?', (tenant, source))
                seq, target = (cur['seq'], cur['gap_target']) if cur else (0, None)
                start_seq = seq
                for event in page:
                    incoming = event.get('seq') if isinstance(event, dict) else None
                    if type(incoming) is int and 0 < incoming <= seq:
                        continue
                    if type(incoming) is not int or incoming <= 0 or incoming != seq + 1:
                        if type(incoming) is not int or incoming <= 0:
                            target = -1  # Unknown sequence: no snapshot watermark can prove coverage.
                        elif target != -1:
                            target = max(incoming, target or 0)
                        flag(db, tenant, source, 'feed', '*',
                             'Unsequenced event requires source audit/operator review'
                             if target == -1 else 'Change-feed gap; authoritative refresh required')
                        gap = True
                        break
                    apply(db, tenant, source, mapping, event.get('kind'), event.get('data'),
                          tag=incoming, error=event.get('error'))
                    try:
                        ts = datetime.fromisoformat(event['changed_at'])
                        age = (datetime.now(timezone.utc) - ts).total_seconds()
                        if ts.tzinfo is None or age > 120 or age < -30:
                            raise ValueError('Source timestamp outside latency target')
                    except (ValueError, TypeError, KeyError):
                        kind, raw = event.get('kind'), event.get('data')
                        names = mapping.get(kind, {}) if isinstance(kind, str) and kind in SCHEMAS else {}
                        field = names.get('parent' if kind == 'line' else 'id')
                        po_id = raw.get(field) if isinstance(raw, dict) and field else None
                        scope = 'po:' + str(po_id) if po_id is not None and str(po_id).strip() else '*'
                        flag(db, tenant, source, 'late:' + str(incoming),
                             scope, 'Late or invalid source event timestamp')
                    seq = incoming
                    if target is not None and target > 0 and incoming >= target:
                        target = None
                        clear(db, tenant, source, 'feed')
                db.execute('''INSERT INTO cursors(tenant,source,seq,checked,gap_target)
                    VALUES(?,?,?,?,?) ON CONFLICT(tenant,source) DO UPDATE SET
                    seq=excluded.seq,checked=excluded.checked,gap_target=excluded.gap_target''',
                    (tenant, source, seq, now(), target))
                if not gap:
                    clear(db, tenant, source, 'poll')
                if not gap and len(page) == limit and seq == start_seq:
                    if requested_seq != start_seq:
                        continue  # Another poll advanced the cursor; refetch before reporting a stall.
                    flag(db, tenant, source, 'pagination', '*',
                         'Full change-feed page made no checkpoint progress')
                    return  # Do not poll the same page forever.
            if gap or len(page) < limit:
                return
    except Exception as exc:
        with store.tx() as db:
            flag(db, tenant, source, 'poll', '*', 'Source unavailable: ' + type(exc).__name__)


def poll_parallel(store, connections):
    with ThreadPoolExecutor(max_workers=min(8, len(connections))) as pool:
        return list(pool.map(lambda x: poll(store, *x), connections))


def run_forever(store, connections, stop, send_alert, interval=20):
    from .monitor import reconcile, freshness
    from .writeback import verify
    from .store import notify_pending
    cycle = 0
    while not stop.is_set():
        poll_parallel(store, connections)
        for tenant, source, client, mapping in connections:
            freshness(store, tenant, source)
            if cycle % 3 == 0:
                reconcile(store, tenant, source, client, mapping)
            with store.tx() as db:
                ops = [r['op_id'] for r in db.execute('''SELECT op_id FROM operations WHERE
                    tenant=? AND source=? AND state='unknown' ''', (tenant, source))]
            for op_id in ops:
                verify(store, op_id, client)
        notify_pending(store, send_alert)
        cycle += 1
        stop.wait(interval)
