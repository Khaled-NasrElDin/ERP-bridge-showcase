import html
import json
from datetime import datetime, timezone
from collections import Counter
from .store import one, record, flag, clear, now
from .integration import SCHEMAS, apply, normalize


def reconcile(store, tenant, source, client, mapping):
    """A snapshot must be complete AND cover any observed feed gap to clear it."""
    try:
        snap = client.snapshot()
        if (not isinstance(snap, dict) or snap.get('complete') is not True or
                not isinstance(snap.get('items'), list) or
                type(snap.get('watermark')) is not int):
            raise ValueError('No complete, consistent source snapshot')
        items, watermark = snap['items'], snap['watermark']
    except Exception as exc:
        with store.tx() as db:
            flag(db, tenant, source, 'snapshot', '*',
                 'Source snapshot unavailable or incomplete: ' + type(exc).__name__)
        return 0

    with store.tx() as db:
        cur = one(db, 'SELECT seq,gap_target FROM cursors WHERE tenant=? AND source=?', (tenant, source))
        unsequenced = bool(cur and cur['gap_target'] == -1)
        target = max(cur['seq'], cur['gap_target'] or 0) if cur else 0
        if watermark < target:
            flag(db, tenant, source, 'snapshot', '*',
                 f'Snapshot watermark {watermark} cannot cover observed sequence {target}')
            return 0

        seen, headers, parents, references = set(), set(), set(), []
        tainted, unknown_scope = set(), False
        safe = True
        # Preflight identity collisions before applying any ambiguous source record.
        owners = {}
        for entry in items:
            kind = entry.get('kind') if isinstance(entry, dict) else None
            raw = entry.get('data') if isinstance(entry, dict) else None
            names = mapping.get(kind, {}) if isinstance(kind, str) and kind in SCHEMAS else {}
            sid = raw.get(names.get('id')) if isinstance(raw, dict) else None
            if sid is not None:
                parent = raw.get(names.get('parent')) if kind == 'line' else sid
                owners.setdefault((kind, str(sid)), []).append(str(parent) if parent is not None else None)
        duplicates = {identity: parents for identity, parents in owners.items() if len(parents) > 1}
        for identity, reported in duplicates.items():
            prior = record(db, tenant, source, *identity)
            affected = set(reported) | ({prior['parent'] or prior['source_id']} if prior else set())
            safe = False
            if None in affected:
                unknown_scope = True
                flag(db, tenant, source, 'snapshot-duplicate:' + str(identity) + ':unknown',
                     '*', 'Duplicate source identity with unknown parent')
            for po in affected - {None}:
                tainted.add(po)
                flag(db, tenant, source, 'snapshot-duplicate:' + str(identity) + ':' + po,
                     'po:' + po, 'Duplicate source identity in snapshot')

        for index, event in enumerate(sorted(items, key=lambda e: e.get('kind') != 'po' if isinstance(e, dict) else True)):
            kind = event.get('kind') if isinstance(event, dict) else None
            raw = event.get('data') if isinstance(event, dict) else None
            if not isinstance(kind, str) or kind not in SCHEMAS:
                flag(db, tenant, source, f'unknown:snapshot:{index}', '*',
                     'Invalid snapshot event type; cannot determine affected PO')
                safe, unknown_scope = False, True
                continue
            names = mapping.get(kind, {}) if isinstance(kind, str) and kind in SCHEMAS else {}
            sid = raw.get(names.get('id')) if isinstance(raw, dict) else None
            parent = raw.get(names.get('parent')) if kind == 'line' and isinstance(raw, dict) else sid
            identity = (kind, str(sid)) if sid is not None else None
            if kind == 'line' and parent is not None:
                parents.add(str(parent))  # Even an invalid line exists; it is PO-scoped.
            if identity in duplicates:
                seen.add(identity)
                continue  # Neither conflicting occurrence may overwrite trusted local data.
            if identity is not None:
                seen.add(identity)
            try:
                data = normalize(kind, raw, mapping)
            except (KeyError, ValueError, TypeError, OverflowError):
                apply(db, tenant, source, mapping, kind, raw, tag=f'snapshot:{index}')
                safe = False
                if parent is not None:
                    tainted.add(str(parent))
                else:
                    unknown_scope = True
                continue
            if kind == 'po':
                headers.add(data['id'])
                references.append((data['id'], data['number']))
            else:
                parents.add(data['parent'])
            prior = record(db, tenant, source, kind, data['id'])
            if not prior or json.loads(prior['body']) != data:
                flag(db, tenant, source, 'drift:' + kind + ':' + data['id'],
                     'po:' + (data.get('parent') or data['id']), 'Snapshot differs from local state')
                apply(db, tenant, source, mapping, kind, raw, tag='snapshot')
            updated = record(db, tenant, source, kind, data['id'])
            if updated and json.loads(updated['body']) == data:
                clear(db, tenant, source, 'drift:' + kind + ':' + data['id'])
                clear(db, tenant, source, 'record:' + kind + ':' + data['id'])
                clear(db, tenant, source, 'missing:' + kind + ':' + data['id'])
            else:
                safe = False  # A PO-scoped issue remains; other POs still reconcile.
                tainted.add(data.get('parent') or data['id'])
        for po_id in headers:
            if po_id not in parents:
                flag(db, tenant, source, 'empty:' + po_id, 'po:' + po_id, 'PO has no lines')
            else:
                clear(db, tenant, source, 'empty:' + po_id)

        if not unknown_scope:  # A malformed PO1 must not hide missing data for PO2.
            for local in db.execute('''SELECT kind,source_id,parent FROM records
                                       WHERE tenant=? AND source=?''', (tenant, source)).fetchall():
                po = local['parent'] or local['source_id']
                if po not in tainted and (local['kind'], local['source_id']) not in seen:
                    flag(db, tenant, source, 'missing:' + local['kind'] + ':' + local['source_id'],
                         'po:' + po, 'Source record missing; deletion requires explicit policy')
        refs = Counter(number for _, number in references)
        if safe:
            for po_id, number in references:
                if refs[number] > 1:
                    flag(db, tenant, source, 'duplicate:' + po_id,
                         'po:' + po_id, 'Duplicate purchase order reference')
                else:
                    clear(db, tenant, source, 'duplicate:' + po_id)
            if not unsequenced:
                db.execute('''INSERT INTO cursors(tenant,source,seq,checked)
                    VALUES(?,?,?,?) ON CONFLICT(tenant,source) DO UPDATE SET
                    seq=excluded.seq,checked=excluded.checked,gap_target=NULL''',
                    (tenant, source, watermark, now()))
                for incident in db.execute('''SELECT code FROM issues WHERE tenant=? AND source=? AND
                                          (code LIKE 'unknown:%' OR code LIKE 'late:%' OR
                                           code LIKE 'snapshot-duplicate:%')''',
                                           (tenant, source)).fetchall():
                    clear(db, tenant, source, incident['code'])
                for name in ('feed', 'snapshot', 'poll', 'pagination'):
                    clear(db, tenant, source, name)
            # A rejected CAS is obsolete only after a complete source refresh proves
            # that the parent/line advanced and no other PO/source issue remains.
            for op in db.execute('''SELECT * FROM operations WHERE tenant=? AND source=?
                                     AND state='conflict' ''', (tenant, source)).fetchall():
                po = record(db, tenant, source, 'po', op['po_id'])
                line = record(db, tenant, source, 'line', op['line_id'])
                advanced = po and line and line['parent'] == op['po_id'] and (
                    po['revision'] > op['po_rev'] or line['revision'] > op['base_rev'])
                other_issue = one(db, '''SELECT 1 FROM issues WHERE tenant=? AND source=?
                    AND code<>? AND scope IN ('*',?) LIMIT 1''',
                    (tenant, source, 'write:' + op['op_id'], 'po:' + op['po_id']))
                if advanced and not other_issue:
                    clear(db, tenant, source, 'write:' + op['op_id'])
                    # The rejected operation stays 'conflict'; only a NEW approved
                    # operation may write against the refreshed revisions.
        # Never clear source-wide gap/snapshot errors on a partially invalid snapshot.
        return len(seen)


def freshness(store, tenant, source, seconds=120, at=None):
    at = at or datetime.now(timezone.utc)
    with store.tx() as db:
        cur = one(db, 'SELECT checked FROM cursors WHERE tenant=? AND source=?', (tenant, source))
        try:
            age = (at - datetime.fromisoformat(cur['checked'])).total_seconds()
        except (TypeError, AttributeError, ValueError):
            age = float('inf')
        if age > seconds:
            flag(db, tenant, source, 'freshness', '*', 'Source has not been checked within 120 seconds')
        else:
            clear(db, tenant, source, 'freshness')
        return age


def status(store, tenant, technical=False):
    with store.tx() as db:
        problems = [dict(r) for r in db.execute('''SELECT source,code,scope,detail,since
                     FROM issues WHERE tenant=? ORDER BY source,code''', (tenant,))]
        pending = [dict(r) for r in db.execute('''SELECT source,op_id,po_id,state
                     FROM operations WHERE tenant=? AND state IN ('unknown','confirmed')''', (tenant,))]
        existing = {(p['source'], p['code']) for p in problems}
        for op in pending:
            if (op['source'], 'write:' + op['op_id']) in existing:
                continue
            problems.append({'source': op['source'], 'code': 'write:' + op['op_id'],
                             'scope': 'po:' + op['po_id'], 'detail': 'Write ' + op['state'], 'since': ''})
        if technical:
            return problems
        result = []
        for p in problems:
            po = p['scope'][3:] if p['scope'].startswith('po:') else None
            header = record(db, tenant, p['source'], 'po', po) if po else None
            reference = json.loads(header['body'])['number'] if header else po
            result.append({'source': p['source'], 'affected': ('Purchase order ' + reference if reference else 'Integration'),
                           'status': 'Being checked', 'message': 'Data needs verification; affected changes are paused'})
        return result


def page(store, tenant, technical=False):
    entries = status(store, tenant, technical)
    title = 'Technical integration incidents' if technical else 'Integration status'
    cells = ''.join('<tr>' + ''.join('<td>' + html.escape(str(v)) + '</td>' for v in entry.values())
                    + '</tr>' for entry in entries)
    return ('<!doctype html><meta charset="utf-8"><title>' + title + '</title><h2>' + title +
            '</h2><p>Active issues: ' + str(len(entries)) + '</p><table border="1" cellpadding="8">'
            + cells + '</table>')
