import logging
import sys
import socket
import errno
import select
import random
import re
import math
import traceback
import time
import contextlib
import fqdns
import ssl
import urlparse
import gevent
import dpkt
from .. import networking
from .. import stat
from ..proxies.http_try import NotHttp
from ..proxies.http_try import HTTP_TRY_PROXY
from ..proxies.http_try import GOOGLE_SCRAMBLER
from ..proxies.http_try import TCP_SCRAMBLER
from ..proxies.http_try import is_blocked_google_host
from ..proxies.http_relay import HttpRelayProxy
from ..proxies.http_connect import HttpConnectProxy
from ..proxies.goagent import GoAgentProxy
from ..proxies.dynamic import DynamicProxy
from ..proxies.shadowsocks import ShadowSocksProxy
from ..proxies.ssh import SshProxy
from .. import us_ip
from .. import lan_ip
from .. import china_ip
from ..proxies.direct import DIRECT_PROXY
from ..proxies.direct import HTTPS_TRY_PROXY
from ..proxies.direct import NONE_PROXY
from .. import ip_substitution
import os.path

TLS1_1_VERSION = 0x0302
RE_HTTP_HOST = re.compile('Host: (.+)')
LOGGER = logging.getLogger(__name__)

proxy_types = {
    'http-relay': HttpRelayProxy,
    'http-connect': HttpConnectProxy,
    'goagent': GoAgentProxy,
    'dynamic': DynamicProxy,
    'ss': ShadowSocksProxy,
    'ssh': SshProxy
}
try:
    from ..proxies.spdy_relay import SpdyRelayProxy
    proxy_types['spdy-relay'] = SpdyRelayProxy
except:
    pass
try:
    from ..proxies.spdy_connect import SpdyConnectProxy
    proxy_types['spdy-connect'] = SpdyConnectProxy
except:
    pass

proxies = []
dns_polluted_at = 0
auto_fix_enabled = True
china_shortcut_enabled = True
direct_access_enabled = True
tcp_scrambler_enabled = True
google_scrambler_enabled = True
goagent_public_servers_enabled = True
ss_public_servers_enabled = True
last_refresh_started_at = -1
force_us_ip = False


class ProxyClient(object):
    def __init__(self, downstream_sock, src_ip, src_port, dst_ip, dst_port):
        super(ProxyClient, self).__init__()
        self.downstream_sock = downstream_sock
        self.downstream_rfile = downstream_sock.makefile('rb', 8192)
        self.downstream_wfile = downstream_sock.makefile('wb', 0)
        self.forward_started = False
        self.resources = [self.downstream_sock, self.downstream_rfile, self.downstream_wfile]
        self.src_ip = src_ip
        self.src_port = src_port
        self.dst_ip = dst_ip
        self.dst_port = dst_port
        self.peeked_data = ''
        self.host = ''
        self.protocol = None
        self.tried_proxies = {}
        self.forwarding_by = None
        self.us_ip_only = force_us_ip
        self.delayed_penalties = []
        self.ip_substituted = False

    def create_tcp_socket(self, server_ip, server_port, connect_timeout):
        upstream_sock = networking.create_tcp_socket(server_ip, server_port, connect_timeout)
        upstream_sock.counter = stat.opened(upstream_sock, self.forwarding_by, self.host, self.dst_ip)
        self.resources.append(upstream_sock)
        self.resources.append(upstream_sock.counter)
        return upstream_sock

    def add_resource(self, res):
        self.resources.append(res)

    def forward(self, upstream_sock, timeout=7, after_started_timeout=360, bufsize=8192, encrypt=None, decrypt=None,
                delayed_penalty=None, on_forward_started=None):

        self.buffer_multiplier = 1
        if self.forward_started:
            if 5228 == self.dst_port: # Google Service
                upstream_sock.settimeout(None)
            else: # More than 5 minutes
                upstream_sock.settimeout(after_started_timeout)
        else:
            upstream_sock.settimeout(timeout)
        self.downstream_sock.settimeout(None)

        def from_upstream_to_downstream():
            try:
                while True:
                    data = upstream_sock.recv(bufsize * self.buffer_multiplier)
                    upstream_sock.counter.received(len(data))
                    self.buffer_multiplier = min(16, self.buffer_multiplier + 1)
                    if data:
                        if not self.forward_started:
                            self.forward_started = True
                            if 5228 == self.dst_port: # Google Service
                                upstream_sock.settimeout(None)
                            else: # More than 5 minutes
                                upstream_sock.settimeout(after_started_timeout)
                            self.apply_delayed_penalties()
                            if on_forward_started:
                                on_forward_started()
                        if decrypt:
                            data = decrypt(data)
                        self.downstream_sock.sendall(data)
                    else:
                        return
            except socket.error as e:
                if e[0] not in (10053, 10054, 10057, errno.EPIPE):
                    return e
            except gevent.GreenletExit:
                return
            except:
                LOGGER.exception('forward u2d failed')
                return sys.exc_info()[1]

        def from_downstream_to_upstream():
            try:
                while True:
                    data = self.downstream_sock.recv(bufsize)
                    self.buffer_multiplier = 1
                    if data:
                        if encrypt:
                            data = encrypt(data)
                        upstream_sock.counter.sending(len(data))
                        upstream_sock.sendall(data)
                    else:
                        return
            except socket.error as e:
                if e[0] not in (10053, 10054, 10057, errno.EPIPE):
                    return e
            except gevent.GreenletExit:
                return
            except:
                LOGGER.exception('forward d2u failed')
                return sys.exc_info()[1]
            finally:
                upstream_sock.close()

        u2d = gevent.spawn(from_upstream_to_downstream)
        d2u = gevent.spawn(from_downstream_to_upstream)
        try:
            e = u2d.join()
            if e:
                raise e
            try:
                upstream_sock.close()
            except:
                pass
            if not self.forward_started:
                self.fall_back(reason='forward does not receive any response', delayed_penalty=delayed_penalty)
        finally:
            try:
                u2d.kill()
            except:
                pass
            try:
                d2u.kill()
            except:
                pass

    def apply_delayed_penalties(self):
        for delayed_penalty in self.delayed_penalties:
            try:
                delayed_penalty()
            except:
                LOGGER.exception('failed to apply delayed penalty: %s' % delayed_penalty)


    def close(self):
        for res in self.resources:
            try:
                res.close()
            except:
                pass

    def fall_back(self, reason, delayed_penalty=None, silently=False):
        if self.forward_started:
            LOGGER.fatal('[%s] fall back can not happen after forward started:\n%s' %
                         (repr(self), traceback.format_stack()))
            raise Exception('!!! fall back can not happen after forward started !!!')
        if delayed_penalty:
            self.delayed_penalties.append(delayed_penalty)
        raise ProxyFallBack(reason, silently=silently)

    def dump_proxies(self):
        LOGGER.info('dump proxies: %s' % [p for p in proxies if not p.died])

    def has_tried(self, proxy):
        if proxy in self.tried_proxies:
            return True
        if isinstance(proxy, DynamicProxy):
            proxy = proxy.delegated_to
        if self.us_ip_only:
            if hasattr(proxy, 'proxy_ip') and not us_ip.is_us_ip(proxy.proxy_ip):
                LOGGER.info('skip %s' % proxy.proxy_ip)
                return True
        return proxy in self.tried_proxies

    def __repr__(self):
        description = '%s:%s => %s:%s' % (self.src_ip, self.src_port, self.dst_ip, self.dst_port)
        if self.host:
            description = '%s %s' % (description, self.host)
        if self.forwarding_by:
            description = '%s %s' % (description, repr(self.forwarding_by))
        return description


class ProxyFallBack(Exception):
    def __init__(self, reason, silently):
        super(ProxyFallBack, self).__init__(reason)
        self.reason = reason
        self.silently = silently


ProxyClient.ProxyFallBack = ProxyFallBack


def handle_client(client):
    try:
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug('[%s] downstream connected' % repr(client))
        pick_proxy_and_forward(client)
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug('[%s] done' % repr(client))
    except NoMoreProxy:
        if HTTP_TRY_PROXY.host_slow_detection_enabled and client.host in HTTP_TRY_PROXY.host_slow_list:
            LOGGER.critical('!!! disable host slow detection !!!')
            HTTP_TRY_PROXY.host_slow_list.clear()
            HTTP_TRY_PROXY.host_slow_detection_enabled = False
        return
    except:
        err_msg = str(sys.exc_info()[1])
        if 'ascii' in err_msg or LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.exception('[%s] done with error' % repr(client))
        else:
            LOGGER.info('[%s] done with error: %s' % (repr(client), err_msg))
    finally:
        client.close()


def pick_proxy_and_forward(client):
    global dns_polluted_at
    if lan_ip.is_lan_traffic(client.src_ip, client.dst_ip):
        try:
            DIRECT_PROXY.forward(client)
        except ProxyFallBack:
            pass
        return
    if client.dst_ip in fqdns.WRONG_ANSWERS:
        LOGGER.error('[%s] destination is GFW wrong answer' % repr(client))
        dns_polluted_at = time.time()
        NONE_PROXY.forward(client)
        return
    if china_shortcut_enabled and china_ip.is_china_ip(client.dst_ip):
        try:
            DIRECT_PROXY.forward(client)
        except ProxyFallBack:
            pass
        return
    if should_fix():
        gevent.spawn(fix_by_refreshing_proxies)
    peek_data(client)
    if china_shortcut_enabled and client.host and fqdns.is_china_domain(client.host):
        try:
            DIRECT_PROXY.forward(client)
        except ProxyFallBack:
            pass
        return
    for i in range(3):
        proxy = pick_proxy(client)
        if not proxy:
            raise NoMoreProxy()
        if 'DIRECT' in proxy.flags:
            LOGGER.debug('[%s] picked proxy: %s' % (repr(client), repr(proxy)))
        else:
            LOGGER.info('[%s] picked proxy: %s' % (repr(client), repr(proxy)))
        try:
            proxy.forward(client)
            return
        except ProxyFallBack as e:
            if not e.silently:
                LOGGER.error('[%s] fall back to other proxy due to %s: %s' % (repr(client), e.reason, repr(proxy)))
            client.tried_proxies[proxy] = e.reason
        except NotHttp:
            try:
                return DIRECT_PROXY.forward(client)
            except client.ProxyFallBack:
                return # give up
    raise NoMoreProxy()


def peek_data(client):
    if not client.peeked_data:
        ins, _, errors = select.select([client.downstream_sock], [], [client.downstream_sock], 0.1)
        if errors:
            LOGGER.error('[%s] peek data failed' % repr(client))
            return DIRECT_PROXY
        if not ins:
            if LOGGER.isEnabledFor(logging.DEBUG):
                LOGGER.debug('[%s] peek data timed out' % repr(client))
        else:
            client.peeked_data = client.downstream_sock.recv(8192)
    protocol, domain = analyze_protocol(client.peeked_data)
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug('[%s] analyzed traffic: %s %s' % (repr(client), protocol, domain))
    client.host = domain
    client.protocol = protocol
    if 'UNKNOWN' == client.protocol:
        if client.dst_port == 80:
            client.protocol = 'HTTP'
        elif client.dst_port == 443:
            client.protocol = 'HTTPS'


class NoMoreProxy(Exception):
    pass


def should_fix():
    if not goagent_public_servers_enabled:
        http_proxies_died = False
    else:
        http_proxies_died = all(proxy.died for proxy in proxies if
                                proxy.is_protocol_supported('HTTP'))
    if not ss_public_servers_enabled:
        https_proxies_died = False
    else:
        https_proxies_died = all(proxy.died for proxy in proxies if
                                 proxy.is_protocol_supported('HTTPS'))
    if auto_fix_enabled and (http_proxies_died or https_proxies_died):
        LOGGER.info('http %s https %s, refresh proxies: %s' %
                    (http_proxies_died, https_proxies_died, proxies))
        return True
    else:
        return False


def pick_proxy(client):
    if client.protocol == 'HTTP':
        return pick_http_try_proxy(client) or pick_proxy_supports(client)
    elif client.protocol == 'HTTPS':
        return pick_https_try_proxy(client) or pick_proxy_supports(client)
    else:
        if pick_proxy_supports(client):
            return pick_https_try_proxy(client) or pick_proxy_supports(client)
        else:
            return DIRECT_PROXY


def analyze_protocol(peeked_data):
    try:
        match = RE_HTTP_HOST.search(peeked_data)
        if match:
            return 'HTTP', match.group(1).strip()
        try:
            ssl3 = dpkt.ssl.SSL3(peeked_data)
        except dpkt.NeedData:
            return 'UNKNOWN', ''
        if ssl3.version in (dpkt.ssl.SSL3_VERSION, dpkt.ssl.TLS1_VERSION, TLS1_1_VERSION):
            return 'HTTPS', parse_sni_domain(peeked_data).strip()
    except:
        LOGGER.exception('failed to analyze protocol')
    return 'UNKNOWN', ''


def parse_sni_domain(data):
    domain = ''
    try:
        # extrace SNI from ClientHello packet, quick and dirty.
        domain = (m.group(2) for m in re.finditer('\x00\x00(.)([\\w\\.]{4,255})', data)
                  if ord(m.group(1)) == len(m.group(2))).next()
    except StopIteration:
        pass
    return domain


def pick_direct_proxy(client):
    return None if DIRECT_PROXY in client.tried_proxies else DIRECT_PROXY


def pick_http_try_proxy(client):
    if client.us_ip_only:
        client.tried_proxies[HTTP_TRY_PROXY] = 'us ip only'
        return None
    if not direct_access_enabled:
        client.tried_proxies[HTTP_TRY_PROXY] = 'direct access disabled'
        return None
    if tcp_scrambler_enabled and not TCP_SCRAMBLER.died:
        if TCP_SCRAMBLER in client.tried_proxies:
            if google_scrambler_enabled and is_blocked_google_host(client.host):
                # give google scrambler a try
                return None if GOOGLE_SCRAMBLER in client.tried_proxies else GOOGLE_SCRAMBLER
            else:
                return None
        else:
            return TCP_SCRAMBLER # first time try
    elif google_scrambler_enabled:
        return None if GOOGLE_SCRAMBLER in client.tried_proxies else GOOGLE_SCRAMBLER
    else:
        return None if HTTP_TRY_PROXY in client.tried_proxies else HTTP_TRY_PROXY


def pick_https_try_proxy(client):
    if client.us_ip_only:
        client.tried_proxies[HTTPS_TRY_PROXY] = 'us ip only'
        return None
    if not direct_access_enabled:
        client.tried_proxies[HTTPS_TRY_PROXY] = 'direct access disabled'
        return None
    return None if HTTPS_TRY_PROXY in client.tried_proxies else HTTPS_TRY_PROXY


def pick_proxy_supports(client):
    supported_proxies = [proxy for proxy in proxies if
                         proxy.is_protocol_supported(client.protocol, client)
                         and not proxy.died and not client.has_tried(proxy)]
    if not supported_proxies:
        return None
    prioritized_proxies = {}
    for proxy in supported_proxies:
        prioritized_proxies.setdefault(proxy.priority, []).append(proxy)
    highest_priority = sorted(prioritized_proxies.keys())[0]
    picked_proxy = random.choice(sorted(prioritized_proxies[highest_priority], key=lambda proxy: proxy.latency)[:3])
    if picked_proxy.latency == 0:
        return random.choice(prioritized_proxies[highest_priority])
    return picked_proxy


def fix_by_refreshing_proxies():
    global auto_fix_enabled
    if refresh_proxies():
        if should_fix():
            LOGGER.critical('!!! auto fix does not work, disable it !!!')
            auto_fix_enabled = False


def refresh_proxies():
    global proxies
    global last_refresh_started_at
    if last_refresh_started_at == -1: # wait for proxy directories to load
        LOGGER.error('skip refreshing proxy because proxy directories not loaded yet')
        return False
    if time.time() - last_refresh_started_at < 60:
        LOGGER.error('skip refreshing proxy after last attempt %s seconds' % (time.time() - last_refresh_started_at))
        return False
    last_refresh_started_at = time.time()
    LOGGER.info('refresh proxies: %s' % proxies)
    socks = []
    type_to_proxies = {}
    for proxy in proxies:
        type_to_proxies.setdefault(proxy.__class__, []).append(proxy)
    success = True
    for proxy_type, instances in type_to_proxies.items():
        try:
            success = success and proxy_type.refresh(instances)
        except:
            LOGGER.exception('failed to refresh proxies %s' % instances)
            success = False
    for sock in socks:
        try:
            sock.close()
        except:
            pass
    LOGGER.info('%s, refreshed proxies: %s' % (success, proxies))
    return success


def check_access_many_times(url, times):
    success = 0
    for i in range(times):
        greenlet = gevent.spawn(check_access, url)
        try:
            if greenlet.get(timeout=10):
                success += 1
                LOGGER.info('checking access %s: passed' % url)
        except:
            LOGGER.error('checking access %s: failed' % url)
        finally:
            greenlet.kill(block=False)
    LOGGER.fatal('checked access %s: %s/%s' % (url, success, times))
    return success


def check_access(url):
    try:
        scheme, netloc, path, _, _, _ = urlparse.urlparse(url)
        ips = networking.resolve_ips(netloc)
        LOGGER.info('resolved %s => %s' % (netloc, ips))
        if not ips:
            return False
        sock = socket.socket()
        sock.settimeout(5)
        try:
            if 'https' == scheme:
                sock = ssl.wrap_socket(sock)
                sock.connect((ips[0], 443))
            else:
                sock.connect((ips[0], 80))
            LOGGER.info('connected to %s via %s' % (netloc, ips[0]))
            request = 'GET %s HTTP/1.1\r\n' \
                      'Host: %s\r\n' \
                      'User-Agent: Mozilla/4.0 (compatible; MSIE 6.0;\r\n\r\n' % (path or '/', netloc)
            LOGGER.info('sent request')
            sock.sendall(request)
            response = sock.recv(8192)
            if 'HTTP' not in response:
                raise Exception('invalid response')
            LOGGER.info('received response')
        finally:
            sock.close()
        return True
    except:
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug('check access %s failed' % url, exc_info=1)
        else:
            LOGGER.info('check access %s failed: %s' % (url, sys.exc_info()[1]))
        return False


def init_proxies(config):
    global last_refresh_started_at
    last_refresh_started_at = -1
    for proxy_id, private_server in config['private_servers'].items():
        try:
            proxy_type = private_server.pop('proxy_type')
            if 'GoAgent' == proxy_type:
                for appid in private_server['appid'].split('|'):
                    if not appid.strip():
                        continue
                    proxy = GoAgentProxy(
                        appid.strip(), private_server.get('path'),
                        private_server.get('goagent_password'))
                    proxy.proxy_id = proxy_id
                    proxies.append(proxy)
            elif 'SSH' == proxy_type:
                for i in range(private_server.get('connections_count') or 4):
                    proxy = SshProxy(
                        private_server['host'], private_server['port'],
                        private_server['username'], private_server.get('password'))
                    proxy.proxy_id = proxy_id
                    proxies.append(proxy)
            elif 'Shadowsocks' == proxy_type:
                proxy = ShadowSocksProxy(
                    private_server['host'], private_server['port'],
                    private_server['password'], private_server['encrypt_method'])
                proxy.proxy_id = proxy_id
                proxies.append(proxy)
            elif 'HTTP' == proxy_type:
                is_secured = 'SSL' == private_server.get('transport_type')
                if 'HTTP' in private_server.get('traffic_type'):
                    proxy = HttpRelayProxy(
                        private_server['host'], private_server['port'],
                        private_server['username'], private_server['password'],
                        is_secured=is_secured)
                    proxy.proxy_id = proxy_id
                    proxies.append(proxy)
                if 'HTTPS' in private_server.get('traffic_type'):
                    proxy = HttpConnectProxy(
                        private_server['host'], private_server['port'],
                        private_server['username'], private_server['password'],
                        is_secured=is_secured)
                    proxy.proxy_id = proxy_id
                    proxies.append(proxy)
            elif 'SPDY' == proxy_type:
                from ..proxies.spdy_relay import SpdyRelayProxy
                from ..proxies.spdy_connect import SpdyConnectProxy
                for i in range(private_server.get('connections_count') or 4):
                    if 'HTTP' in private_server.get('traffic_type'):
                        proxy = SpdyRelayProxy(
                            private_server['host'], private_server['port'], 'auto',
                            private_server['username'], private_server['password'])
                        proxy.proxy_id = proxy_id
                        proxies.append(proxy)
                    if 'HTTPS' in private_server.get('traffic_type'):
                        proxy = SpdyConnectProxy(
                            private_server['host'], private_server['port'], 'auto',
                            private_server['username'], private_server['password'])
                        proxy.proxy_id = proxy_id
                        proxies.append(proxy)
            else:
                raise NotImplementedError('proxy type: %s' % proxy_type)
        except:
            LOGGER.exception('failed to init %s' % private_server)
    try:
        success = False
        for i in range(8):
            if load_public_proxies(config['public_servers']):
                last_refresh_started_at = 0
                if refresh_proxies():
                    success = True
                    break
            retry_interval = math.pow(2, i)
            LOGGER.error('refresh failed, will retry %s seconds later' % retry_interval)
            gevent.sleep(retry_interval)
        if success:
            LOGGER.critical('proxies init successfully')
            us_ip_cache_file = None
            if config['config_file']:
                us_ip_cache_file = os.path.join(os.path.dirname(config['config_file']), 'us_ip')
            us_ip.load_cache(us_ip_cache_file)
            for proxy in proxies:
                if isinstance(proxy, DynamicProxy):
                    proxy = proxy.delegated_to
                if hasattr(proxy, 'proxy_ip'):
                    us_ip.is_us_ip(proxy.proxy_ip)
            us_ip.save_cache(us_ip_cache_file)
            # if config['access_check_enabled']:
            #     LOGGER.info('check access in 10 seconds')
            #     gevent.sleep(10)
            #     check_access_many_times('https://twitter.com', 5)
            #     check_access_many_times('https://plus.google.com', 3)
            #     check_access_many_times('http://www.youtube.com', 3)
            #     check_access_many_times('http://www.facebook.com', 3)
        else:
            LOGGER.critical('proxies init failed')
    except:
        LOGGER.exception('failed to init proxies')


def load_public_proxies(public_servers):
    try:
        sock = networking.create_udp_socket()
        more_proxies = []
        with contextlib.closing(sock):
            sock.settimeout(10)
            request = dpkt.dns.DNS(
                id=random.randint(1, 65535),
                qd=[dpkt.dns.DNS.Q(name=str(public_servers['source']), type=dpkt.dns.DNS_TXT)])
            sock.sendto(str(request), ('8.8.8.8', 53))
            gevent.sleep(0.1)
            for an in dpkt.dns.DNS(sock.recv(1024)).an:
                priority, proxy_type, count, partial_dns_record = an.text[0].split(':')[:4]
                count = int(count)
                priority = int(priority)
                if public_servers.get('%s_enabled' % proxy_type) and proxy_type in proxy_types:
                    for i in range(count):
                        dns_record = '%s.fqrouter.com' % partial_dns_record.replace('#', str(i + 1))
                        more_proxies.append(DynamicProxy(dns_record=dns_record, type=proxy_type, priority=priority))
        proxies.extend(more_proxies)
        LOGGER.info('loaded public servers: %s' % public_servers)
        return True
    except:
        LOGGER.exception('failed to load proxy from directory')
        return False


def clear_proxy_states():
    global last_refresh_started_at
    last_refresh_started_at = 0
    HTTP_TRY_PROXY.host_black_list.clear()
    HTTP_TRY_PROXY.host_slow_list.clear()
    HTTP_TRY_PROXY.host_slow_detection_enabled = True
    HTTP_TRY_PROXY.dst_black_list.clear()
    TCP_SCRAMBLER.bad_requests.clear()
    HTTPS_TRY_PROXY.dst_black_list.clear()
    ip_substitution.sub_map.clear()
    for proxy in proxies:
        proxy.clear_latency_records()
        proxy.clear_failed_times()
    GoAgentProxy.last_refresh_started_at = 0
    GoAgentProxy.gray_list = set()
    GoAgentProxy.black_list = set()
    GoAgentProxy.google_ip_failed_times = {}
    GoAgentProxy.google_ip_latency_records = {}
    stat.counters = []