"""Public HTTPS only, DNS-pinned connections, no redirects or environment proxies."""
import http.client
import ipaddress
import re
import socket
import ssl
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit


def validate_base_url(url):
    if not isinstance(url,str) or len(url)>2048 or any(ord(c)<=32 or ord(c)==127 for c in url):
        raise ValueError('INVALID_MODEL_ENDPOINT')
    parsed=urlsplit(url)
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port==0 or '%' in parsed.hostname
            or not re.fullmatch(r'[A-Za-z0-9._~/-]*',parsed.path) or '..' in parsed.path.split('/')):
        raise ValueError('INVALID_MODEL_ENDPOINT')
    return parsed


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        rows=socket.getaddrinfo(self.host,self.port,type=socket.SOCK_STREAM)
        if not rows or any(not ipaddress.ip_address(row[4][0]).is_global for row in rows):
            raise ValueError('MODEL_ENDPOINT_NOT_PUBLIC')
        deadline=time.monotonic()+self.timeout
        last=None
        for index,(family,kind,proto,_,address) in enumerate(rows):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('MODEL_CONNECT_TIMEOUT')
            raw=socket.socket(family,kind,proto)
            raw.settimeout(remaining/(len(rows)-index))
            try:raw.connect(address)
            except OSError as exc:raw.close();last=exc;continue
            try:
                raw.settimeout(max(.01,deadline-time.monotonic()))
                self.sock=self._context.wrap_socket(raw,server_hostname=self.host)
                self.sock.settimeout(self.timeout)
                return
            except Exception:raw.close();raise
        raise OSError('MODEL_CONNECT_FAILED') from last


class Response:
    def __init__(self,connection,response):
        self.connection,self.response=connection,response
        self.status,self.headers=response.status,response.headers
    def read1(self,size):return self.response.read1(size)
    def readline(self,size=-1):return self.response.readline(size)
    def __enter__(self):return self
    def __exit__(self,*args):
        self.response.close();self.connection.close()


def open_model_request(request,timeout=90):
    parsed=validate_base_url(request.full_url)
    connection=PinnedHTTPS(parsed.hostname,parsed.port or 443,timeout=timeout,context=ssl.create_default_context())
    try:
        connection.request('POST',parsed.path or '/',body=request.data,headers=dict(request.header_items()))
        response=connection.getresponse()
        if not 200<=response.status<300:
            status=response.status;headers=response.headers
            response.close();connection.close()
            raise HTTPError(request.full_url,status,'Model endpoint rejected request',headers,None)
        return Response(connection,response)
    except Exception:connection.close();raise
