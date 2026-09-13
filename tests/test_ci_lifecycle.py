"""Snapshot checks must restore data and compare exact results before cleanup."""
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('fault', ['', 'count', 'query'])
def test_snapshot_check_restores_and_compares_data(monkeypatch, tmp_path, fault):
    calls = []

    class Response:
        status_code = 200
        content = b'synthetic snapshot bytes'

        def __init__(self, result):
            self.result = result

        def raise_for_status(self):
            pass

        def json(self):
            return {'result': self.result}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['headers'] == {'api-key': 'test-only'}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            calls.append(('get', url))
            if '/snapshots/' in url:
                return Response({})
            count = 0 if fault == 'count' and '/ci-restore-' in url else 7
            return Response({'points_count': count})

        def post(self, url, **kwargs):
            calls.append(('post', url))
            if '/snapshots/upload' in url:
                assert kwargs['files']['snapshot'][1] == Response.content
                assert url.endswith('?priority=snapshot')
                return Response({})
            if url.endswith('/points/scroll'):
                assert kwargs['json']['with_vector'] is True
                return Response({'points': [{'vector': {'dense': [1.0, 0.0]}}]})
            if url.endswith('/points/query'):
                assert kwargs['json']['params']['exact'] is True
                assert kwargs['json']['query'] == [1.0, 0.0]
                ids = [9] if fault == 'query' and '/ci-restore-' in url else [1, 2]
                return Response({'points': [{'id': i} for i in ids]})
            assert url.endswith('/snapshots')
            return Response({'name': 'synthetic.snapshot'})

        def delete(self, url):
            calls.append(('delete', url))
            assert '/ci-restore-' in url
            return Response({})

    settings = SimpleNamespace(qdrant_url='http://qdrant', qdrant_collection='synthetic', qdrant_api_key='test-only')
    monkeypatch.setitem(sys.modules, 'httpx2', SimpleNamespace(Client=Client))
    monkeypatch.setitem(sys.modules, 'mainframe_rag.config', SimpleNamespace(load_settings=lambda: settings))
    script = (ROOT / 'scripts/ci/check_lifecycle.sh').read_text().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    executable = tmp_path / "snapshot_check.py"
    executable.write_text(script)
    if fault:
        with pytest.raises(AssertionError):
            runpy.run_path(str(executable))
    else:
        runpy.run_path(str(executable))
    assert any(method == 'post' and '/snapshots/upload' in url for method, url in calls)
    assert calls[-1][0] == 'delete'
