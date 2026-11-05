from datetime import date
from uuid import uuid4
from .store import one, record, blocked, flag, clear


class Conflict(Exception):
    """Adapter signals an authoritative version conflict."""


REQUIRED_WRITE_CAPABILITIES = (
    'atomic_version_check', 'durable_idempotency', 'receipt_lookup',
    'po_aggregate_cas'
)


def request(store, tenant, source, client, line_id, eta, *, approved=False, op_id=None):
    if approved is not True:
        raise PermissionError('Approved, authenticated business workflow required')
    eta = date.fromisoformat(eta).isoformat()
    line_id, op_id = str(line_id), op_id or str(uuid4())
    declared = getattr(client, 'write_capabilities', {})
    if not getattr(client, 'safe_write', False) or not all(
        declared.get(name) is True for name in REQUIRED_WRITE_CAPABILITIES
    ):
        raise ValueError('Automated write-back disabled: required ERP safety capabilities missing')
    with store.tx() as db:
        old = one(db, 'SELECT * FROM operations WHERE op_id=?', (op_id,))
        if old:
            if (old['tenant'], old['source'], old['line_id'], old['eta']) != (tenant, source, line_id, eta):
                raise ValueError('Operation ID reused with changed request')
            return old['state']  # Never resubmit the same intent.
        line = record(db, tenant, source, 'line', line_id)
        if not line:
            raise ValueError('PO line not synchronized')
        po = record(db, tenant, source, 'po', line['parent'])
        verified = one(db, 'SELECT checked FROM cursors WHERE tenant=? AND source=?', (tenant, source))
        from datetime import datetime, timezone
        if not verified or not verified['checked'] or (datetime.now(timezone.utc) -
                datetime.fromisoformat(verified['checked'])).total_seconds() > 120:
            raise ValueError('Source has not been checked recently')
        if not po or blocked(db, tenant, source, line['parent']):
            raise ValueError('Incomplete PO or unresolved source/PO problem')
        active = one(db, '''SELECT 1 FROM operations WHERE tenant=? AND source=?
                      AND po_id=? AND state IN ('unknown','confirmed')''',
                     (tenant, source, line['parent']))
        if active:
            raise ValueError('PO has an unreconciled write')
        db.execute('''INSERT INTO operations(op_id,tenant,source,line_id,po_id,eta,base_rev,po_rev,state)
                      VALUES(?,?,?,?,?,?,?,?,'unknown')''',
                   (op_id, tenant, source, line_id, line['parent'], eta,
                    line['revision'], po['revision']))
    try:
        receipt = client.write_eta(line_id, eta, line['revision'], po['revision'], op_id)
    except Conflict:
        with store.tx() as db:
            db.execute("UPDATE operations SET state='conflict' WHERE op_id=?", (op_id,))
            flag(db, tenant, source, 'write:' + op_id, 'po:' + line['parent'],
                 'ERP rejected change: PO or line revision differs')
        return 'conflict'
    except Exception:
        with store.tx() as db:
            flag(db, tenant, source, 'write:' + op_id, 'po:' + line['parent'],
                 'Outcome unknown; do not blindly resend')
        return 'unknown'
    return _receipt(store, op_id, receipt)


def _receipt(store, op_id, receipt):
    with store.tx() as db:
        op = one(db, 'SELECT * FROM operations WHERE op_id=?', (op_id,))
        valid = (isinstance(receipt, dict) and
                 (receipt.get('id'), receipt.get('eta'), receipt.get('base_rev'),
                  receipt.get('po_rev')) ==
                 (op['line_id'], op['eta'], op['base_rev'], op['po_rev']) and
                 type(receipt.get('rev')) is int and receipt['rev'] > op['base_rev'])
        if not valid:
            flag(db, op['tenant'], op['source'], 'write:' + op_id,
                 'po:' + op['po_id'], 'ERP receipt does not match submitted operation')
            return 'unknown'
        current = record(db, op['tenant'], op['source'], 'line', op['line_id'])
        if current and current['revision'] >= receipt['rev']:
            import json
            body = json.loads(current['body'])
            if current['revision'] == receipt['rev']:
                state = 'settled' if body['eta'] == op['eta'] else 'conflict'
            else:
                state = 'superseded'  # Accepted write, followed by a newer ERP revision.
        else:
            state = 'confirmed'  # Remote accepted; local feed has not caught up.
        db.execute('UPDATE operations SET state=?,remote_rev=? WHERE op_id=?',
                   (state, receipt['rev'], op_id))
        if state == 'conflict':
            flag(db, op['tenant'], op['source'], 'write:' + op_id,
                 'po:' + op['po_id'], 'ERP contradicted accepted write at same revision')
        else:
            clear(db, op['tenant'], op['source'], 'write:' + op_id)
        return state


def verify(store, op_id, client):
    with store.tx() as db:
        op = one(db, 'SELECT * FROM operations WHERE op_id=?', (op_id,))
        if not op:
            raise ValueError('Unknown operation ID')
        if op['state'] != 'unknown':
            return op['state']
    try:
        receipt = client.lookup(op_id)
    except Exception:
        return 'unknown'
    return _receipt(store, op_id, receipt) if receipt is not None else 'unknown'
