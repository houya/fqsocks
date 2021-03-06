import logging
import urlparse

import gevent.server

from .. import networking
from .proxy_client import ProxyClient
from .proxy_client import handle_client
from ..proxies.http_try import recv_till_double_newline
from ..proxies.http_try import parse_request
from .. import httpd
import fqlan
import httplib
import jinja2
import os

LOGGER = logging.getLogger(__name__)
WHITELIST_PAC_FILE = os.path.join(os.path.dirname(__file__), '..', 'templates', 'whitelist.pac')
dns_cache = {}
LISTEN_IP = None
LISTEN_PORT = None
server_greenlet = None

@httpd.http_handler('GET', 'pac')
def pac_page(environ, start_response):
    with open(WHITELIST_PAC_FILE) as f:
        template = jinja2.Template(unicode(f.read(), 'utf8'))
    ip = fqlan.get_default_interface_ip()
    start_response(httplib.OK, [('Content-Type', 'application/x-ns-proxy-autoconfig')])
    return [template.render(http_gateway='%s:2516' % ip).encode('utf8')]


def handle(downstream_sock, address):
    src_ip, src_port = address
    request, payload = recv_till_double_newline('', downstream_sock)
    if not request:
        return
    method, path, headers = parse_request(request)
    if 'CONNECT' == method.upper():
        if ':' in path:
            dst_host, dst_port = path.split(':')
            dst_port = int(dst_port)
        else:
            dst_host = path
            dst_port = 443
        dst_ip = resolve_ip(dst_host)
        if not dst_ip:
            return
        downstream_sock.sendall('HTTP/1.1 200 OK\r\n\r\n')
        client = ProxyClient(downstream_sock, src_ip, src_port, dst_ip, dst_port)
        handle_client(client)
    else:
        dst_host = urlparse.urlparse(path)[1]
        if ':' in dst_host:
            dst_host, dst_port = dst_host.split(':')
            dst_port = int(dst_port)
        else:
            dst_port = 80
        dst_ip = resolve_ip(dst_host)
        if not dst_ip:
            return
        client = ProxyClient(downstream_sock, src_ip, src_port, dst_ip, dst_port)
        request_lines = ['%s %s HTTP/1.1\r\n' % (method, path[path.find(dst_host) + len(dst_host):])]
        headers.pop('Proxy-Connection', None)
        headers['Host'] = dst_host
        headers['Connection'] = 'close'
        for key, value in headers.items():
            request_lines.append('%s: %s\r\n' % (key, value))
        request = ''.join(request_lines)
        client.peeked_data = request + '\r\n' + payload
        handle_client(client)


def resolve_ip(host):
    if host in dns_cache:
        return dns_cache[host]
    ips = networking.resolve_ips(host)
    if ips:
        ip = ips[0]
    else:
        ip = None
    dns_cache[host] = ip
    return dns_cache[host]


def serve_forever():
    server = gevent.server.StreamServer((LISTEN_IP, LISTEN_PORT), handle)
    LOGGER.info('started fqsocks http gateway at %s:%s' % (LISTEN_IP, LISTEN_PORT))
    try:
        server.serve_forever()
    except:
        LOGGER.exception('failed to start http gateway')
    finally:
        LOGGER.info('http gateway stopped')

