"""Thread cap, listener lifecycle and bounded session bookkeeping without Qt."""
import http.client
import json
from pathlib import Path
import socket
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import monitor_server as server


def main():
    service = server.Service('127.0.0.1', 0)
    results, sockets = [], []
    try:
        assert server.local_address('192.168.1.5') and server.local_address('172.16.1.5')
        assert not server.local_address('0.0.0.0') and not server.local_address('8.8.8.8')
        results.append('listener permits explicit LAN/loopback addresses, not public or wildcard binds')
        for i in range(4):
            connection = socket.create_connection(service.http.server_address, timeout=2)
            connection.sendall(b'GET / HTTP/1.1\r\n')
            sockets.append(connection)
        deadline = time.monotonic()+1
        while service.http.slots._value and time.monotonic()<deadline:
            time.sleep(.01)
        assert service.http.slots._value == 0
        extra = socket.create_connection(service.http.server_address, timeout=2)
        extra.sendall(b'GET / HTTP/1.1\r\n')
        try:
            assert extra.recv(1) == b''
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        extra.close()
        assert service.http.slots._value == 0
        for connection in sockets:
            connection.close()
        sockets.clear()
        deadline = time.monotonic()+3
        while service.http.slots._value < 4 and time.monotonic()<deadline:
            time.sleep(.01)
        assert service.http.slots._value == 4
        results.append('four slow connections cap worker count; excess rejected and slots recover')
        service.publish({'intensity': 123, 'parameters': {'ExposureTime': 20000}})
        for i in range(40):
            service.snapshot(-1, 'phone{:08d}'.format(i))
        assert len(service.clients) == 16
        with service.lock:
            service.clients = {key: time.monotonic()-10 for key in service.clients}
        assert service.active_clients() == 0
        results.append('client bookkeeping bounded to16; inactive viewers expire')
        service.set_preview({'monotonic_s': 42, 'data_url': 'data:image/jpeg;base64,'})
        old_mono, old_perf = time.monotonic, time.perf_counter
        try:
            time.monotonic, time.perf_counter = lambda: 1000, lambda: 47
            assert service.snapshot(-1, 'clockcheck1')['frame']['age_s'] == 5
        finally:
            time.monotonic, time.perf_counter = old_mono, old_perf
        service.set_preview(None)
        results.append('frame age uses acquisition clock even when monotonic epochs differ')
        conn = http.client.HTTPConnection(*service.http.server_address, timeout=2)
        conn.request('GET','/api/state?client=realphone01', headers={'Authorization':'Bearer '+service.token})
        response = conn.getresponse()
        assert response.status == 200 and json.loads(response.read())['intensity'] == 123
        conn.close()
        results.append('monitor remains usable after overload')
    finally:
        for connection in sockets:
            connection.close()
        service.stop()
        assert service.stopped.wait(3)
    report={'status':'passed','cases':results}
    (Path(__file__).parent/'monitor_server_report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':
    main()
