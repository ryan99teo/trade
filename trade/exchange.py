"""Upbit exchange API: fixed endpoints, HS512 authentication, no withdrawal routes."""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote
from urllib.request import Request, urlopen
import uuid


class ExchangeError(Exception):
    def __init__(self, message, code="network_error", definite=False):
        super().__init__(message)
        self.code, self.definite = code, definite


def token(access, secret, params):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value,separators=(",",":")).encode()).rstrip(b"=")
    payload={"access_key":access,"nonce":str(uuid.uuid4())}
    if params:
        query=unquote(urlencode(params,doseq=True))
        payload.update(query_hash=hashlib.sha512(query.encode()).hexdigest(),query_hash_alg="SHA512")
    signing=encode({"alg":"HS512","typ":"JWT"})+b"."+encode(payload)
    signature=base64.urlsafe_b64encode(hmac.new(secret.encode(),signing,hashlib.sha512).digest()).rstrip(b"=")
    return (signing+b"."+signature).decode()


class UpbitExchange:
    def __init__(self, access="", secret="", enabled=False):
        self._access,self._secret=access.strip(),secret.strip()
        self.configured=bool(self._access and self._secret)
        self.enabled=bool(enabled)
        self.binding=hashlib.sha256(self._access.encode()).hexdigest() if self.configured else None
        self._lock=threading.Lock()
        self._next_request=0

    def configure(self, access=None, secret=None, enabled=None):
        if access is not None:
            self._access = access.strip()
        if secret is not None:
            self._secret = secret.strip()
        if enabled is not None:
            self.enabled = bool(enabled)
        self.configured = bool(self._access and self._secret)
        self.binding = hashlib.sha256(self._access.encode()).hexdigest() if self.configured else None

    @classmethod
    def from_env(cls):
        # Kept for tests/tools; the app loads credentials from the live DB UI settings.
        return cls(os.getenv("UPBIT_ACCESS_KEY",""),os.getenv("UPBIT_SECRET_KEY",""),
                   os.getenv("TRADE_LIVE_ENABLED","false").lower()=="true")

    def request(self, method, path, params=None, public=False):
        allowed={("GET","/v1/orders/chance"),("GET","/v1/order"),("POST","/v1/orders"),
                 ("POST","/v1/orders/test"),("DELETE","/v1/order"),("GET","/v1/orderbook/instruments")}
        if (method,path) not in allowed or (public and path!="/v1/orderbook/instruments"):
            raise ExchangeError("허용하지 않는 업비트 경로입니다.","unsupported",True)
        if not public and not self.configured:
            raise ExchangeError("업비트 API 키 설정이 필요합니다.","not_configured",True)
        if method=="POST" and path=="/v1/orders" and not self.enabled:
            raise ExchangeError("실거래 활성화 설정이 필요합니다.","disabled",True)
        params=params or {}
        headers={"Accept":"application/json","User-Agent":"personal-btc-live/0.1"}
        if not public:headers["Authorization"]="Bearer "+token(self._access,self._secret,params)
        url="https://api.upbit.com"+path
        body=None
        if method=="POST":
            body=json.dumps(params,separators=(",",":")).encode()
            headers["Content-Type"]="application/json"
        elif params:url+="?"+urlencode(params,doseq=True)
        with self._lock:
            wait=self._next_request-time.monotonic()
            if wait>2:
                raise ExchangeError("업비트 호출 제한 대기 중입니다.","rate_limited",True)
            if wait>0:time.sleep(wait)
            self._next_request=time.monotonic()+.15
            try:
                with urlopen(Request(url,data=body,headers=headers,method=method),timeout=8) as response:
                    remaining=response.headers.get("Remaining-Req","")
                    if "sec=0" in remaining:self._next_request=time.monotonic()+1.2
                    return json.loads(response.read(2000000),parse_float=str)
            except HTTPError as exc:
                code="http_{}".format(exc.code)
                try:
                    raw=json.loads(exc.read(65536))
                    candidate=raw.get("error",{}).get("name","")
                    if isinstance(candidate,str) and candidate.replace("_","").isalnum() and len(candidate)<80:
                        code=candidate
                except (ValueError,TypeError,AttributeError):pass
                if exc.code in (418,429):
                    try:delay=max(60 if exc.code==418 else 1,float(exc.headers.get("Retry-After","60")))
                    except (ValueError,TypeError):delay=60
                    self._next_request=time.monotonic()+min(delay,86400)
                # Never expose response bodies, Authorization or URL-bearing exceptions.
                raise ExchangeError("업비트 API 오류: "+code,code,400<=exc.code<500) from None
            except (URLError,OSError,ValueError):
                raise ExchangeError("업비트 요청 결과를 확인할 수 없습니다.") from None

    def chance(self):return self.request("GET","/v1/orders/chance",{"market":"KRW-BTC"})
    def instrument(self):return self.request("GET","/v1/orderbook/instruments",{"markets":"KRW-BTC"},public=True)
    def order(self, identifier):return self.request("GET","/v1/order",{"identifier":identifier})
    def place(self, params):return self.request("POST","/v1/orders",params)
    def test(self, params):return self.request("POST","/v1/orders/test",params)
    def cancel(self, identifier):return self.request("DELETE","/v1/order",{"identifier":identifier})
