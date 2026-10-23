"""Exercise the new read-only tools through the same HTTP transport agents use."""
import asyncio
import json
import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from decompnet.app import agents, tools
from decompnet.network import Network


def test_reference_audit_and_progress_over_http(tmp_path):
    net = Network(tmp_path / 'test.db')
    net.resolve_function = lambda agent, slug, name: name
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id,slug,name,root,arch,config) VALUES(1,'ps1','PS1',?,'mips',?)",
                   (str(tmp_path), json.dumps({'platform': 'psx', 'compiler': {'toolchain': 'gcc-2.7.2-psx'}})))
        db.execute("INSERT INTO context_versions(project_id,version,header) VALUES(1,1,'int callee(int value);')")
        for name, status, source in [('caller', 'open', 'int caller(void) { return callee(); }'),
                                     ('callee', 'matched', 'int callee(int value) { return value; }')]:
            db.execute("INSERT INTO functions(project_id,name,status,size,n_insns,target,relocs,asm,"
                       "best_source,matched_source,best_score) VALUES(1,?,?,8,2,?,'[]',?,?,?,?)",
                       (name, status, b'\x08\x00\xe0\x03\x00\x00\x00\x00',
                        'jr $ra\n nop', source, source if status == 'matched' else None,
                        0 if status == 'matched' else 100))
    agent = net.authenticate(net.register_agent('reader'))
    before = net.store.conn.total_changes
    server = None
    thread = None
    sock = socket.socket()
    try:
        mcp = tools.build_mcp(net, lambda ctx: (agent, 'ps1'))
        app = mcp.streamable_http_app(host='127.0.0.1')
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, 'HTTP server did not start'
            time.sleep(.01)

        async def exercise():
            async with httpx2.AsyncClient(timeout=15) as http:
                async with Client(streamable_http_client(f'http://127.0.0.1:{port}/mcp', http_client=http)) as client:
                    names = {t.name for t in (await client.list_tools()).tools}
                    required = {'find_reference_examples', 'audit_call_boundaries', 'progress_report'}
                    assert required <= names
                    assert required <= {s['function']['name'] for s in agents.api_tool_schemas()}
                    assert not required.intersection(s['function']['name'] for s in agents.api_tool_schemas(True))
                    for name, arguments, expected in [
                        ('find_reference_examples', {'function': 'caller', 'query': 'callee'}, 'recorded byte match'),
                        ('audit_call_boundaries', {'function': 'caller'}, 'arity'),
                        ('progress_report', {'hours': 1}, 'Agent-hours unavailable'),
                    ]:
                        result = await client.call_tool(name, arguments)
                        text = ''.join(c.text for c in result.content if getattr(c, 'text', None))
                        assert not result.is_error, text
                        assert expected in text, text
                    invalid = await client.call_tool('progress_report', {'hours': -1})
                    assert invalid.is_error

        asyncio.run(exercise())
        assert net.store.conn.total_changes == before
        assert net.store.one('SELECT COUNT(*) n FROM claims')['n'] == 0
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(5)
            assert not thread.is_alive()
        sock.close()
        net.store.conn.close()
        net._pool.shutdown()


@pytest.mark.parametrize('hours', [0, -1, 169, float('nan'), float('inf')])
def test_progress_window_rejected_before_database_access(hours):
    from decompnet.network import NetworkError
    with pytest.raises(NetworkError, match='hours'):
        tools._progress_report(None, 'ps1', hours)
