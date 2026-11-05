"""Smoke test for route preservation, with the application database outside the repo."""
import os
from pathlib import Path
import subprocess
import sys


def test_main_preserves_v1_routes(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["ERP_BRIDGE_DATABASE_URL"] = f"sqlite:///{(tmp_path / 'inventory.sqlite').as_posix()}"
    env["ERP_BRIDGE_PO_SQLITE_PATH"] = str(tmp_path / 'po.sqlite')
    code = '''
from fastapi.testclient import TestClient
from main import app
with TestClient(app) as client:
    assert client.get('/health').json() == {'status': 'ok', 'service': 'ERP Bridge'}
    assert client.get('/inventory').status_code == 200
    assert client.get('/console').status_code == 200
    paths = set(app.openapi()['paths'])
    for path in ('/integration/sync', '/integration/normalized',
                 '/tenants/{tenant_id}/inventory', '/tenants/{tenant_id}/sync-page',
                 '/tenants/{tenant_id}/resume-sync',
                 '/tenants/{tenant_id}/sync-runs/{run_id}/pages/{page}',
                 '/po/tenants/{tenant_id}/sources/{source_id}/status'):
        assert path in paths, path
    assert "get" in app.openapi()["paths"]["/health"]
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=backend, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
