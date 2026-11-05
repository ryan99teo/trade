"""Telegram transport and durable outbox delivery. No credentials in persisted data."""
import asyncio
from datetime import datetime
import hashlib
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .market import UTC


class DeliveryError(Exception):
    def __init__(self, message, retry_after=30, permanent=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


class Telegram:
    def __init__(self, token="", chat_id=""):
        self._token = ""
        self._chat_id = ""
        self.enabled = False
        self.recipient = None
        self.missing = ["봇 토큰", "채팅 ID"]
        self.configure(token=token, chat_id=chat_id)

    @classmethod
    def from_env(cls):
        return cls(os.getenv("TRADE_TELEGRAM_BOT_TOKEN",""), os.getenv("TRADE_TELEGRAM_CHAT_ID",""))

    def configure(self, token=None, chat_id=None):
        if token is not None:
            self._token = str(token).strip()
        if chat_id is not None:
            self._chat_id = str(chat_id).strip()
        self.enabled = bool(self._token and self._chat_id)
        self.recipient = (hashlib.sha256((self._token + "\n" + self._chat_id).encode()).hexdigest()
                          if self.enabled else None)
        self.missing = [label for value, label in ((self._token, "봇 토큰"), (self._chat_id, "채팅 ID")) if not value]
        return self

    def send(self, text):
        if not self.enabled:
            raise DeliveryError("텔레그램 연결 설정이 필요합니다.", permanent=True)
        payload = json.dumps({"chat_id": self._chat_id, "text": text[:4000]}).encode()
        request = Request("https://api.telegram.org/bot"+self._token+"/sendMessage", data=payload,
                          headers={"Content-Type":"application/json"}, method="POST")
        try:
            with urlopen(request,timeout=8) as response:
                data = json.loads(response.read(65536))
        except HTTPError as exc:
            delay = 30
            if exc.code == 429:
                try:
                    delay = max(1,min(86400,int(json.loads(exc.read(65536)).get("parameters",{}).get("retry_after",30))))
                except (ValueError,TypeError,AttributeError):
                    pass
            # Never propagate urllib exception text: its URL contains the bot token.
            raise DeliveryError("텔레그램 HTTP 오류 ({})".format(exc.code),delay,
                                permanent=400<=exc.code<500 and exc.code!=429) from None
        except (URLError, OSError, ValueError):
            raise DeliveryError("텔레그램 연결 또는 응답 오류") from None
        if not isinstance(data,dict) or data.get("ok") is not True:
            raise DeliveryError("텔레그램이 메시지를 수락하지 않았습니다.")
        result = data.get("result",{})
        if not isinstance(result,dict) or not isinstance(result.get("message_id"),int):
            raise DeliveryError("텔레그램 메시지 확인 응답이 올바르지 않습니다.")
        return result["message_id"]


def restore(store, telegram):
    """Apply UI-saved credentials from the paper ledger. Returns True when DB had values."""
    token = store.get("telegram_bot_token")
    chat_id = store.get("telegram_chat_id")
    if token is None and chat_id is None:
        return False
    telegram.configure(token=token or "", chat_id=chat_id or "")
    return True


def save(store, telegram, *, enabled, bot_token=None, chat_id=None, timestamp=None):
    """Persist Telegram credentials in the paper DB. Blank inputs keep the previous secret."""
    timestamp = timestamp or datetime.now(UTC).isoformat()
    if not enabled:
        with store.db:
            store.set("telegram_bot_token", "")
            store.set("telegram_chat_id", "")
            store.event(timestamp, "telegram", "텔레그램 알림 설정 변경: 끔")
        telegram.configure(token="", chat_id="")
        store.notifications_recipient = None
        return status(store, telegram)
    token = (bot_token if bot_token is not None and str(bot_token).strip()
             else store.get("telegram_bot_token", "") or "")
    chat = (chat_id if chat_id is not None and str(chat_id).strip()
            else store.get("telegram_chat_id", "") or "")
    token, chat = str(token).strip(), str(chat).strip()
    if not token or not chat:
        raise ValueError("텔레그램을 켜려면 봇 토큰과 채팅 ID가 필요합니다.")
    with store.db:
        store.set("telegram_bot_token", token)
        store.set("telegram_chat_id", chat)
        store.event(timestamp, "telegram", "텔레그램 알림 설정 변경: 켬 · 자격 증명 저장됨")
    telegram.configure(token=token, chat_id=chat)
    store.notifications_recipient = telegram.recipient
    return status(store, telegram)


def status(store, telegram):
    recipient = telegram.recipient
    rows = []
    counts = {}
    if recipient:
        rows = [dict(row) for row in store.db.execute(
            "SELECT id,kind,created_at,status,attempts,last_error,sent_at FROM notifications WHERE recipient=? ORDER BY id DESC LIMIT 10",
            (recipient,))]
        counts = {row["status"]: row["n"] for row in store.db.execute(
            "SELECT status,COUNT(*) AS n FROM notifications WHERE recipient=? GROUP BY status", (recipient,))}
    token_set = bool((store.get("telegram_bot_token") or "").strip())
    chat_set = bool((store.get("telegram_chat_id") or "").strip())
    return {"enabled": telegram.enabled, "missing": telegram.missing, "recent": rows,
            "token_set": token_set, "chat_id_set": chat_set,
            "keys_set": token_set and chat_set,
            "pending": counts.get("pending", 0), "failed": counts.get("failed", 0),
            "sent": counts.get("sent", 0)}


async def deliver_one(store, telegram, now=None):
    if not telegram.enabled:
        return False
    now = time.time() if now is None else now
    row = store.db.execute("SELECT * FROM notifications WHERE recipient=? AND status='pending' AND next_attempt<=? ORDER BY id LIMIT 1",
                           (telegram.recipient,now)).fetchone()
    if row is None:
        return False
    attempts = row["attempts"]+1
    try:
        message_id = await asyncio.to_thread(telegram.send, row["message"]+"\n알림 #"+str(row["id"]))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        known = isinstance(exc,DeliveryError)
        error = str(exc) if known else "텔레그램 전송 처리 오류"
        permanent = (known and exc.permanent) or attempts>=5
        delay = max(30*2**(attempts-1),exc.retry_after if known else 30)
        with store.db:
            store.db.execute("UPDATE notifications SET status=?,attempts=?,next_attempt=?,last_error=? WHERE id=?",
                             ("failed" if permanent else "pending",attempts,now+delay,error,row["id"]))
    else:
        with store.db:
            store.db.execute("UPDATE notifications SET status='sent',attempts=?,sent_at=?,last_error=NULL,message_id=? WHERE id=?",
                             (attempts,datetime.now(UTC).isoformat(),str(message_id),row["id"]))
    return True


async def delivery_loop(store, telegram):
    while True:
        try:
            await deliver_one(store,telegram)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A DB fault must not crash collection or expose transport credentials.
            pass
        await asyncio.sleep(1.2)
