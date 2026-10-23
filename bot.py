"""Monitor X accounts for usage-limit resets and notify Telegram/Discord. Secrets stay in local .env."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv, set_key
from web_source import WebClient, WebFetchError
from ai_interpreter import AIError, Analysis, CACHE_VERSION, PRODUCTS, CodexAnalyzer, text_of, validate
from time_rules import explicit_time, relative_minutes

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
LOG = logging.getLogger("reset-bot")
HTTP = requests.Session()


@dataclass
class Assessment:
    relevant: bool
    when: datetime | None = None
    detail: str = "시간 미정 — 원문 확인 필요"


def assess(text: str, created: datetime, source_zone: str = "America/Los_Angeles") -> Assessment:
    """Conservative rules: preserve ambiguous prose instead of inventing a timestamp."""
    t = text.lower().replace("’", "'")
    if not re.search(r"\breset(?:s|ting)?\b|리셋|초기화", t):
        return Assessment(False)
    # This dedicated account also writes short announcements without saying Codex.
    if re.search(r"password|factory reset|reset (?:my|your) (?:phone|computer)|비밀번호", t):
        return Assessment(False)
    if re.search(r"\b(?:no|not|never)\s+(?:\w+\s+){0,3}reset|won't\s+(?:\w+\s+){0,3}reset|cancelled|canceled|취소", t):
        return Assessment(True, detail="리셋 취소/부정 관련 글 — 원문 확인 필요")
    if re.search(r"\b(?:already|just)\s+(?:\w+\s+){0,2}reset|\breset\s+(?:is\s+)?(?:done|complete)|\bhave reset\b|\bwe reset\b|완료", t):
        return Assessment(True, detail="이미 리셋되었다는 내용 — 예정 시간 없음")
    if "?" in t or re.search(r"\bmight\b|\bmaybe\b|\bperhaps\b", t):
        return Assessment(True, detail="질문/가능성 표현 포함 — 확정 일정으로 해석하지 않음")
    relative = re.search(r"\bin\s+.+", t)
    if relative:
        minutes = relative_minutes(relative[0])
        if minutes is not None:
            return Assessment(True, created + timedelta(minutes=minutes), "게시 시각 기준 상대시간 (예정/변경 가능)")
    try:
        when = explicit_time(text, created)
    except (ValueError, OverflowError):
        when = None
    if when is not None:
        return Assessment(True, when, "원문 명시 시각 (예정/변경 가능)")
    if "tomorrow" in t:
        return Assessment(True, detail="원문: tomorrow — 정확한 시각·시간대 미기재")
    return Assessment(True)


class ApiError(RuntimeError):
    def __init__(self, service: str, status: int = 0, retry_after: int = 60):
        super().__init__(f"{service} 요청 실패 (HTTP {status or 'network'}). 토큰·권한·한도·네트워크를 확인하세요.")
        self.retry_after = retry_after
        self.service = service
        self.status = status


def retry_seconds(value):
    try:
        return min(max(math.ceil(float(value)), 60), 86400)
    except (ValueError, TypeError, OverflowError):
        return 60


def request_json(service: str, method: str, url: str, **kwargs):
    try:
        response = HTTP.request(method, url, timeout=(10, 30), **kwargs)
    except requests.RequestException:
        # requests exceptions can contain Telegram's secret-bearing URL.
        raise ApiError(service) from None
    if not response.ok:
        retry = retry_seconds(response.headers.get("Retry-After", 60))
        if response.status_code == 429:
            try:
                body = response.json()
                if isinstance(body, dict):
                    parameters = body.get("parameters")
                    hint = parameters.get("retry_after") if isinstance(parameters, dict) else body.get("retry_after")
                    retry = max(retry, retry_seconds(hint))
            except ValueError:
                pass
        raise ApiError(service, response.status_code, retry)
    try:
        data = response.json()
    except ValueError:
        raise ApiError(service) from None
    if isinstance(data, dict) and data.get("ok") is False:
        parameters = data.get("parameters")
        retry = parameters.get("retry_after") if isinstance(parameters, dict) else 60
        raise ApiError(service, data.get("error_code", 0), retry_seconds(retry))
    if not isinstance(data, dict) or data.get("errors"):
        raise ApiError(service)
    return data


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = chat_id
        self.destination = hashlib.sha256((self.base + chat_id).encode()).hexdigest()

    def send(self, text: str):
        request_json("Telegram", "POST", self.base + "sendMessage", json={"chat_id": self.chat_id, "text": text, "link_preview_options": {"is_disabled": True}})

    def chats(self):
        data = request_json("Telegram", "GET", self.base + "getUpdates", params={"timeout": 0})
        return sorted({str(item["message"]["chat"]["id"]) for item in data.get("result", []) if item.get("message", {}).get("chat", {}).get("type") == "private"})


def chunks(text: str, limit: int = 1900):
    """Split on line breaks within Discord's 2000-unit content limit (UTF-16, emoji-safe)."""
    size = lambda s: len(s.encode("utf-16-le")) // 2
    parts, current = [], ""
    for line in text.split("\n"):
        while size(line) > limit:
            cut = limit
            while size(line[:cut]) > limit:
                cut -= 1
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:cut])
            line = line[cut:]
        candidate = f"{current}\n{line}" if current else line
        if current and size(candidate) > limit:
            parts.append(current)
            candidate = line
        current = candidate
    if current.strip():
        parts.append(current)
    return parts


class Discord:
    def __init__(self, webhook_url: str):
        if not re.fullmatch(r"https://(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+", webhook_url):
            raise ValueError("DISCORD_WEBHOOK_URL 형식이 올바르지 않습니다.")
        self.url = webhook_url
        self.destination = hashlib.sha256(webhook_url.encode()).hexdigest()

    def send(self, text: str):
        for part in chunks(text):
            self.send_part(part)

    def send_part(self, part):
        # wait=true returns JSON; flags=4 hides link previews; never ping @everyone/roles.
        request_json("Discord", "POST", self.url, params={"wait": "true"}, json={"content": part, "flags": 4, "allowed_mentions": {"parse": []}})


class Notifier:
    """Send to every channel; on retry, skip channels that already received the same text."""
    def __init__(self, channels, store=None):
        self.channels = channels
        self.store = store
        self.delivered = set()

    def send(self, text: str, event_id=None):
        persistent = self.store is not None and event_id is not None
        if persistent:
            event = self.store.enqueue(event_id, text)
            if event["state"] in {"cancelled", "expired"}:
                return
            text = event["text"]
        digest = hashlib.sha256(text.encode()).hexdigest()
        errors = []
        for index, channel in enumerate(self.channels):
            destination = channel.destination if isinstance(channel, (Telegram, Discord)) else str(index)
            parts = chunks(text, 1900 if isinstance(channel, Discord) else 3900) if isinstance(channel, (Discord, Telegram)) else [text]
            try:
                for part_index, part in enumerate(parts):
                    key = (digest, index, part_index)
                    if (self.store.received(event_id, destination, part_index) if persistent else key in self.delivered):
                        continue
                    if persistent:
                        limit = self.store.db.execute("SELECT until_ts FROM channel_limits WHERE destination=?", (destination,)).fetchone()
                        if limit and limit[0] > time.time():
                            raise ApiError(type(channel).__name__, 429, math.ceil(limit[0] - time.time()))
                    if isinstance(channel, Discord):
                        channel.send_part(part)
                    else:
                        channel.send(part)
                    if persistent:
                        self.store.receipt(event_id, destination, part_index)
                    else:
                        self.delivered.add(key)
            except ApiError as exc:
                LOG.error("%s", exc)
                if persistent and exc.status == 429:
                    with self.store.db:
                        self.store.db.execute("INSERT INTO channel_limits VALUES (?,?) ON CONFLICT(destination) DO UPDATE SET until_ts=MAX(until_ts,excluded.until_ts)", (destination, time.time() + exc.retry_after))
                errors.append(exc)
        if errors:
            raise max(errors, key=lambda e: e.retry_after)
        if persistent:
            self.store.finish_event(event_id)
        else:
            self.delivered = {key for key in self.delivered if key[0] != digest}


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS cursor (account TEXT PRIMARY KEY, post_id TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (account TEXT, post_id TEXT, PRIMARY KEY(account, post_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS web_monitor (account TEXT PRIMARY KEY, cutoff TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS posts (account TEXT, post_id TEXT, created_ts REAL, body TEXT, PRIMARY KEY(account,post_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS analyses (account TEXT, post_id TEXT, fingerprint TEXT, result TEXT, PRIMARY KEY(account,post_id))")
        # Reset posts without a time: keep checking the thread for the author's later replies.
        self.db.execute("CREATE TABLE IF NOT EXISTS followups (account TEXT, post_id TEXT, until_ts REAL, body TEXT, PRIMARY KEY(account,post_id))")
        self.db.execute("CREATE INDEX IF NOT EXISTS posts_account_created ON posts(account, created_ts)")
        self.db.execute("CREATE TABLE IF NOT EXISTS retries (account TEXT, post_id TEXT, phase TEXT, attempts INTEGER, next_ts REAL, error TEXT, PRIMARY KEY(account,post_id,phase))")
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox (event_id TEXT PRIMARY KEY, text TEXT, metadata TEXT, created_ts REAL, finished_ts REAL, state TEXT NOT NULL DEFAULT 'pending')")
        if "state" not in {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}:
            self.db.execute("ALTER TABLE outbox ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'")
            self.db.execute("UPDATE outbox SET state='sent' WHERE finished_ts IS NOT NULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (event_id TEXT, destination TEXT, part INTEGER, sent_ts REAL, PRIMARY KEY(event_id,destination,part))")
        self.db.execute("CREATE TABLE IF NOT EXISTS channel_limits (destination TEXT PRIMARY KEY, until_ts REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS runtime (account TEXT PRIMARY KEY, state TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS health (label TEXT PRIMARY KEY, state TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS schedules (account TEXT, product TEXT, post_id TEXT, when_ts REAL, revision TEXT, active INTEGER, observed_ts REAL NOT NULL DEFAULT 0, PRIMARY KEY(account,product,post_id))")
        if "observed_ts" not in {row[1] for row in self.db.execute("PRAGMA table_info(schedules)")}:
            self.db.execute("ALTER TABLE schedules ADD COLUMN observed_ts REAL NOT NULL DEFAULT 0")
        self.db.commit()

    def enqueue(self, event_id, text, metadata=None):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO outbox(event_id,text,metadata,created_ts) VALUES (?,?,?,?)", (event_id, text, json.dumps(metadata, ensure_ascii=False), time.time()))
        return self.event(event_id)

    def event(self, event_id):
        row = self.db.execute("SELECT text,metadata,finished_ts,state,created_ts FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        return {"text": row[0], "metadata": json.loads(row[1]), "finished": row[2] is not None, "state": row[3], "created_ts": row[4]} if row else None

    def pending_followup(self, account, post_id):
        prefix = f"followup:{account}:{post_id}:"
        row = self.db.execute("SELECT event_id FROM outbox WHERE substr(event_id,1,?)=? AND finished_ts IS NULL ORDER BY created_ts LIMIT 1", (len(prefix), prefix)).fetchone()
        return (row[0], self.event(row[0])) if row else (None, None)

    def received(self, event_id, destination, part):
        return self.db.execute("SELECT 1 FROM receipts WHERE event_id=? AND destination=? AND part=?", (event_id, destination, part)).fetchone() is not None

    def receipt(self, event_id, destination, part):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?,?)", (event_id, destination, part, time.time()))

    def finish_event(self, event_id, state="sent"):
        with self.db:
            self.db.execute("UPDATE outbox SET finished_ts=?,state=? WHERE event_id=? AND state='pending'", (time.time(), state, event_id))

    def failed(self, account, post_id, phase, exc, now=None):
        now = time.time() if now is None else now
        row = self.db.execute("SELECT attempts FROM retries WHERE account=? AND post_id=? AND phase=?", (account, post_id, phase)).fetchone()
        attempts = (row[0] if row else 0) + 1
        delay = max(getattr(exc, "retry_after", 300), min(60 * 2 ** min(attempts, 6), 3600))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO retries VALUES (?,?,?,?,?,?)", (account, post_id, phase, attempts, now + delay, json.dumps({"type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False)))

    def retry_ready(self, account, post_id, phase, now=None):
        row = self.db.execute("SELECT next_ts FROM retries WHERE account=? AND post_id=? AND phase=?", (account, post_id, phase)).fetchone()
        return row is None or row[0] <= (time.time() if now is None else now)

    def clear_retry(self, account, post_id, phase):
        with self.db:
            self.db.execute("DELETE FROM retries WHERE account=? AND post_id=? AND phase=?", (account, post_id, phase))

    def ai_failure(self, account):
        for row in self.db.execute("SELECT error FROM retries WHERE account=?", (account,)):
            error = json.loads(row[0])
            if error["type"] == "AIError":
                return error["message"]
        return None

    def runtime(self, account, **changes):
        row = self.db.execute("SELECT state FROM runtime WHERE account=?", (account,)).fetchone()
        state = json.loads(row[0]) if row else {}
        state.update(changes)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runtime VALUES (?,?)", (account, json.dumps(state, ensure_ascii=False)))
        return state

    def update_schedule(self, account, product, post, result, observed_ts=None):
        observed_ts = time.time() if observed_ts is None else observed_ts
        parent = post.get("in_reply_to_id")
        linked = self.db.execute("SELECT post_id FROM schedules WHERE account=? AND product=? AND post_id=?", (account, product, parent)).fetchone() if parent else None
        if linked and not result.relevant:
            return
        post_id = parent if linked else post["id"]
        if not result.relevant and self.db.execute("SELECT 1 FROM schedules WHERE account=? AND product=? AND post_id=?", (account, product, post_id)).fetchone() is None:
            return
        previous = self.db.execute("SELECT observed_ts FROM schedules WHERE account=? AND product=? AND post_id=?", (account, product, post_id)).fetchone()
        if previous and previous[0] > observed_ts:
            return
        when = result.when.timestamp() if result.relevant and result.when is not None else None
        revision = hashlib.sha256(f"{account}:{product}:{post_id}:{when}".encode()).hexdigest()[:24]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO schedules VALUES (?,?,?,?,?,?,?)", (account, product, post_id, when, revision, int(when is not None), observed_ts))
            prefix = f"reminder:{account}:{product}:{post_id}:"
            current = prefix + revision + ":"
            self.db.execute("UPDATE outbox SET finished_ts=?,state='cancelled' WHERE state='pending' AND substr(event_id,1,?)=? AND substr(event_id,1,?)!=?", (time.time(), len(prefix), prefix, len(current), current))
            self.db.execute("DELETE FROM retries WHERE phase='reminder' AND substr(post_id,1,?)=? AND substr(post_id,1,?)!=?", (len(prefix), prefix, len(current), current))

    def prune(self, days, now=None):
        """Prune old completed data only. Keep unseen posts, watches and unsent events."""
        cutoff = (time.time() if now is None else now) - days * 86400
        with self.db:
            self.db.execute("DELETE FROM analyses WHERE EXISTS (SELECT 1 FROM posts p JOIN seen s ON s.account=p.account AND s.post_id=p.post_id WHERE p.account=analyses.account AND p.post_id=analyses.post_id AND p.created_ts<?) AND NOT EXISTS (SELECT 1 FROM followups f WHERE f.account=analyses.account AND f.post_id=analyses.post_id)", (cutoff,))
            self.db.execute("DELETE FROM posts WHERE created_ts<? AND EXISTS (SELECT 1 FROM seen s WHERE s.account=posts.account AND s.post_id=posts.post_id) AND NOT EXISTS (SELECT 1 FROM followups f WHERE f.account=posts.account AND f.post_id=posts.post_id)", (cutoff,))
            self.db.execute("DELETE FROM receipts WHERE event_id IN (SELECT event_id FROM outbox WHERE finished_ts<?)", (cutoff,))
            self.db.execute("DELETE FROM outbox WHERE finished_ts<?", (cutoff,))
            self.db.execute("DELETE FROM schedules WHERE when_ts<?", (cutoff,))
            self.db.execute("DELETE FROM schedules WHERE active=0 AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.account=schedules.account AND p.post_id=schedules.post_id) AND NOT EXISTS (SELECT 1 FROM followups f WHERE f.account=schedules.account AND f.post_id=schedules.post_id)")

    def watch(self, account, post, until: datetime):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO followups VALUES (?,?,?,?)", (account, post["id"], until.timestamp(), json.dumps(post, ensure_ascii=False)))

    def watched(self, account, now: datetime):
        with self.db:
            self.db.execute("DELETE FROM followups WHERE account=? AND until_ts<? AND NOT EXISTS (SELECT 1 FROM retries r WHERE r.account=followups.account AND r.post_id=followups.post_id AND r.phase='followup') AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.finished_ts IS NULL AND substr(o.event_id,1,length('followup:'||followups.account||':'||followups.post_id||':'))='followup:'||followups.account||':'||followups.post_id||':')", (account, now.timestamp()))
        return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM followups WHERE account=? ORDER BY CAST(post_id AS INTEGER)", (account,))]

    def unwatch(self, account, post_id):
        with self.db:
            self.db.execute("DELETE FROM followups WHERE account=? AND post_id=?", (account, post_id))

    def remember(self, account, posts):
        with self.db:
            for post in posts:
                created = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00")).timestamp()
                self.db.execute("INSERT INTO posts VALUES (?,?,?,?) ON CONFLICT(account,post_id) DO UPDATE SET created_ts=excluded.created_ts,body=excluded.body", (account, post["id"], created, json.dumps(post, ensure_ascii=False)))

    def pending(self, account, cutoff, ready_only=False):
        rows = self.db.execute("SELECT body FROM posts p WHERE account=? AND created_ts>=? AND NOT EXISTS (SELECT 1 FROM seen s WHERE s.account=p.account AND s.post_id=p.post_id) AND (?=0 OR NOT EXISTS (SELECT 1 FROM retries r WHERE r.account=p.account AND r.post_id=p.post_id AND r.phase='process' AND r.next_ts>?)) ORDER BY created_ts, CAST(post_id AS INTEGER)", (account, cutoff.timestamp(), int(ready_only), time.time())).fetchall()
        return [json.loads(row[0]) for row in rows]

    def context(self, account, target):
        stamp = datetime.fromisoformat(target["created_at"].replace("Z", "+00:00")).timestamp()
        rows = self.db.execute("SELECT body FROM posts WHERE account=? AND post_id!=? AND created_ts<=? AND created_ts>=? ORDER BY created_ts DESC LIMIT 8", (account, target["id"], stamp, stamp - 72 * 3600)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def analysis(self, account, post, analyzer, context, cache=True, product="codex"):
        inputs = [CACHE_VERSION, product, post, context, getattr(analyzer, "model", ""), (ROOT / "ai_instructions.txt").read_text(encoding="utf-8"), (ROOT / "ai_schema.json").read_text(encoding="utf-8")]
        fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        row = self.db.execute("SELECT result FROM analyses WHERE account=? AND post_id=? AND fingerprint=?", (account, post["id"], fingerprint)).fetchone()
        if row:
            LOG.info("게시글 %s: 저장된 Codex 해석 사용", post["id"])
            try:
                return validate(json.loads(row[0]), post, context)
            except (AIError, ValueError):
                LOG.warning("게시글 %s: 유효하지 않은 캐시를 다시 분석합니다.", post["id"])
        result = analyzer.analyze(post, context, product)
        if cache:
            self.runtime(account, ai_ok=time.time())
        if cache:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO analyses VALUES (?,?,?,?)", (account, post["id"], fingerprint, json.dumps(result.data, ensure_ascii=False)))
        return result

    def cutoff(self, account: str, initial: datetime) -> datetime:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO web_monitor VALUES (?, ?)", (account, initial.isoformat()))
        return datetime.fromisoformat(self.db.execute("SELECT cutoff FROM web_monitor WHERE account=?", (account,)).fetchone()[0])

    def has_seen(self, account: str, post_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE account=? AND post_id=?", (account, post_id)).fetchone() is not None

    def get(self, account: str):
        row = self.db.execute("SELECT post_id FROM cursor WHERE account=?", (account,)).fetchone()
        return row[0] if row else None

    def advance(self, account: str, post_id: str):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (account, post_id))
            previous = self.get(account)
            if previous is None or int(post_id) > int(previous):
                self.db.execute("INSERT OR REPLACE INTO cursor VALUES (?, ?)", (account, post_id))


def message(username: str, post: dict, result: Assessment, display_zone: str, product: str = "codex") -> str:
    when = result.when.astimezone(ZoneInfo(display_zone)).strftime("%Y-%m-%d %H:%M %Z") if result.when else "미정"
    text = post.get("note_post", post.get("note_tweet", {})).get("text") or post["text"]
    return f"🔔 {PRODUCTS[product][0]} 리셋 관련 게시글\n예정 시간: {when}\n{result.detail}\n출처: @{username}\nhttps://x.com/{username}/status/{post['id']}\n\n{text[:2800]}"


def with_replies(post: dict) -> str:
    replies = "\n".join(f"↳ {text_of(r)}" for r in post.get("replies", []))
    return text_of(post) + (f"\n\n[작성자 추가 답글]\n{replies}" if replies else "")


def ai_message(username: str, post: dict, result: Analysis, display_zone: str, product: str = "codex", follow_up: bool = False) -> str:
    data = result.data
    statuses = {"scheduled": "리셋 예고", "changed": "일정 변경", "completed": "완료 주장", "cancelled": "취소", "uncertain": "확인 필요", "unrelated": "관련 없음"}
    confidence = {"high": "높음", "medium": "보통", "low": "낮음"}
    when = result.when.astimezone(ZoneInfo(display_zone)).strftime("%Y-%m-%d %H:%M %Z") if result.when else "미정 / 확정 시각 없음"
    created = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00")).astimezone(ZoneInfo(display_zone)).strftime("%Y-%m-%d %H:%M %Z")
    original = with_replies(post)
    if len(original) > 1400:
        original = original[:1400] + "… [긴 원문은 링크에서 확인]"
    evidence = " / ".join(f'“{e["quote"][:180]}” (글 {e["post_id"]})' for e in data["evidence"][:2]) or "직접 근거 없음"
    # Bound every section so Telegram's UTF-16 4096-unit limit also works with emoji.
    head = "🔁 [작성자 후속 답글 반영] " if follow_up else "🔔 "
    body = (f"{head}{PRODUCTS[product][0]} 리셋 소식 · {statuses[data['status']]}\n\n"
            f"원본 메시지 :\n{original}\n\n"
            f"AI 해석 :\n{data['interpretation'][:700]}\n\n"
            f"예정 시간 : {when}\n"
            f"판단 확신도 : {confidence[data['confidence']]} (AI 판단)\n"
            f"근거 : {evidence}\n"
            f"확인할 점 : {data['uncertainty'][:250] or '예정은 변경될 수 있으므로 원문을 확인하세요.'}")
    footer = f"\n\n게시 시각 : {created}\n출처 : @{username}\nhttps://x.com/{username}/status/{post['id']}"
    if len(body.encode("utf-16-le")) // 2 > 3600:
        body = body.encode("utf-16-le")[:7000].decode("utf-16-le", errors="ignore") + "\n[긴 내용 일부 생략]"
    return body + footer


FOLLOW_UP_HOURS = 6


def enrich(post, thread, username):
    """Full text and the author's thread replies; fall back to the profile copy if the page fails."""
    if thread is None:
        return post
    try:
        full, replies = thread(post["id"])
    except WebFetchError as exc:
        LOG.warning("@%s 글 %s 상세 페이지 확인 실패 — 프로필 본문으로 진행: %s", username, post["id"], str(exc).splitlines()[0])
        return post
    return {**post, **full, "replies": replies}


def needs_follow_up(result) -> bool:
    return result.relevant and result.data.get("status") in {"scheduled", "changed", "uncertain"}


def watch_until(post, result):
    created = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
    end = created + timedelta(hours=FOLLOW_UP_HOURS)
    if result.when:
        end = max(end, result.when + timedelta(hours=2))
    return min(end, created + timedelta(days=7))


class BatchError(RuntimeError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__(f"{len(errors)}개 항목 처리 실패. 개별 항목은 보관하고 재시도합니다.")


def raise_errors(errors):
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BatchError(errors)


def notice_metadata(post, result, context):
    return {"post": post, "context": context, "analysis": result.data if isinstance(result, Analysis) else None,
            "when": result.when.isoformat() if result.when else None,
            "relevant": result.relevant, "detail": getattr(result, "detail", "")}


def restore_notice(record):
    meta = record["metadata"]
    post = meta["post"]
    result = validate(meta["analysis"], post, meta["context"]) if meta["analysis"] else Assessment(meta["relevant"], datetime.fromisoformat(meta["when"]) if meta["when"] else None, meta["detail"])
    return post, result


def process(posts, username, store, notifier, display_zone, source_zone, dry_run=False, analyzer=None, max_posts=10, product="codex", thread=None):
    count, attempted, errors = 0, 0, []
    for post in sorted(posts, key=lambda item: int(item["id"])):
        if store.has_seen(username, post["id"]):
            continue
        if attempted >= max_posts:
            break
        attempted += 1
        event_id = f"post:{username}:{product}:{post['id']}"
        try:
            queued = None if dry_run else store.event(event_id)
            if queued and queued["metadata"]:
                post, result = restore_notice(queued)
                notice = queued["text"]
            else:
                post = enrich(post, thread, username)
                context = store.context(username, post)
                if analyzer is not None:
                    result = store.analysis(username, post, analyzer, context, cache=not dry_run, product=product)
                    LOG.info("게시글 %s: Codex 분류=%s", post["id"], result.data["status"])
                else:
                    result = assess(with_replies(post), datetime.fromisoformat(post["created_at"].replace("Z", "+00:00")), source_zone)
                notice = ai_message(username, post, result, display_zone, product) if analyzer is not None else message(username, post, result, display_zone, product)
                if result.relevant and not dry_run:
                    queued = store.enqueue(event_id, notice, notice_metadata(post, result, context))
            if not dry_run:
                # Stop stale reminders as soon as a change/cancellation is validated,
                # even if delivering its notification is currently failing.
                store.update_schedule(username, product, post, result, queued["created_ts"] if queued else None)
            if result.relevant:
                if dry_run:
                    print(notice + "\n")
                else:
                    notifier.send(notice, event_id=event_id)
            if not dry_run:
                # Receipts precede the seen marker: a crash before advance can safely retry.
                if analyzer is not None and needs_follow_up(result):
                    store.watch(username, post, watch_until(post, result))
                store.advance(username, post["id"])
                store.clear_retry(username, post["id"], "process")
            count += 1
        except (ApiError, AIError, WebFetchError) as exc:
            LOG.error("@%s 글 %s: %s", username, post["id"], exc)
            if not dry_run:
                store.failed(username, post["id"], "process", exc)
            errors.append(exc)
    raise_errors(errors)
    return count


def follow_up(username, store, notifier, display_zone, analyzer, product, thread, now=None):
    """Re-open watched posts; when the author adds replies, re-analyze and send an update."""
    sent, errors = 0, []
    now = now or datetime.now(UTC)
    for post in store.watched(username, now):
        if not store.retry_ready(username, post["id"], "followup", now.timestamp()):
            continue
        try:
            event_id, queued = store.pending_followup(username, post["id"])
            if queued:
                post, result = restore_notice(queued)
                notice = queued["text"]
            else:
                full, replies = thread(post["id"])
                updated = {**post, **full, "replies": replies}
                if updated == dict(post, replies=post.get("replies", [])):
                    store.clear_retry(username, post["id"], "followup")
                    continue
                post = updated
                context = store.context(username, post)
                result = store.analysis(username, post, analyzer, context, product=product)
                event_id = f"followup:{username}:{post['id']}:" + hashlib.sha256(json.dumps(post, sort_keys=True).encode()).hexdigest()
                notice = ai_message(username, post, result, display_zone, product, follow_up=True)
                if result.relevant:
                    queued = store.enqueue(event_id, notice, notice_metadata(post, result, context))
            store.update_schedule(username, product, post, result, queued["created_ts"] if queued else None)
            if result.relevant:
                notifier.send(notice, event_id=event_id)
                sent += 1
            if needs_follow_up(result):
                store.watch(username, post, watch_until(post, result))
            else:
                store.unwatch(username, post["id"])
            store.clear_retry(username, post["id"], "followup")
        except (WebFetchError, ApiError, AIError) as exc:
            LOG.warning("@%s 글 %s 후속 확인 실패: %s", username, post["id"], exc)
            store.failed(username, post["id"], "followup", exc, now.timestamp())
            errors.append(exc)
    raise_errors(errors)
    return sent


def send_reminders(store, notifier, display_zone, minutes, now=None):
    now = now or datetime.now(UTC)
    sent, errors = 0, []
    for account, product, post_id, when_ts, revision in store.db.execute("SELECT account,product,post_id,when_ts,revision FROM schedules WHERE active=1").fetchall():
        # No stale catch-up reminders: tolerate one short network retry / scheduler delay.
        for offset in sorted(set(minutes), reverse=True):
            due = when_ts - offset * 60
            event_id = f"reminder:{account}:{product}:{post_id}:{revision}:{offset}"
            if now.timestamp() > due + 180:
                store.finish_event(event_id, "expired")
                store.clear_retry(account, event_id, "reminder")
                continue
            if now.timestamp() < due:
                continue
            if not store.retry_ready(account, event_id, "reminder", now.timestamp()):
                continue
            if (store.event(event_id) or {}).get("finished"):
                continue
            when = datetime.fromtimestamp(when_ts, UTC).astimezone(ZoneInfo(display_zone)).strftime("%Y-%m-%d %H:%M %Z")
            label = f"약 {offset}분 전" if offset else "예정 시각 도달"
            text = f"⏰ {PRODUCTS[product][0]} 리셋 예정 재알림 · {label}\n예정 시간: {when}\n실제 완료 확인이 아닌 원문 일정 알림입니다.\nhttps://x.com/{account}/status/{post_id}"
            try:
                notifier.send(text, event_id=event_id)
                store.clear_retry(account, event_id, "reminder")
                sent += 1
            except ApiError as exc:
                store.failed(account, event_id, "reminder", exc, now.timestamp())
                errors.append(exc)
        if now.timestamp() > when_ts + 180:
            with store.db:
                store.db.execute("UPDATE schedules SET active=0 WHERE account=? AND product=? AND post_id=? AND revision=?", (account, product, post_id, revision))
    raise_errors(errors)
    return sent


class HealthNotice:
    """One failure/recovery notice per outage; retry notice delivery when sending fails."""
    def __init__(self, label="X 웹 감시", store=None, repeat_seconds=3600):
        self.notified = False
        self.label = label
        self.store = store
        self.repeat_seconds = repeat_seconds
        self.state = {"notified": False, "since": None, "last_notice": None, "outage": None, "sequence": 0}
        if store is not None:
            row = store.db.execute("SELECT state FROM health WHERE label=?", (label,)).fetchone()
            if row:
                self.state.update(json.loads(row[0]))
                self.notified = self.state["notified"]

    def update(self, notifier, error=None, now=None):
        now = time.time() if now is None else now
        if notifier is None:
            return
        if error is not None and self.state["since"] is None:
            self.state.update(since=now, outage=uuid.uuid4().hex)
        repeat = error is not None and self.notified and self.state["last_notice"] is not None and now - self.state["last_notice"] >= self.repeat_seconds
        if error is not None and self.notified and not repeat:
            return
        if error is None and self.state["since"] is None and not self.notified:
            return
        text = (f"⚠️ {self.label} 오류\n" + str(error) + "\n해당 글은 처리 완료로 넘기지 않고 재시도합니다.") if error else f"✅ {self.label}가 복구되었습니다."
        if repeat:
            text = f"⚠️ {self.label} 장애 지속 ({int((now - self.state['since']) // 60)}분)\n{error}"
        event_id = f"health:{self.label}:{self.state['outage']}:{self.state['sequence']}:{bool(error)}"
        if error is None and self.store is not None:
            self.store.finish_event(f"health:{self.label}:{self.state['outage']}:{self.state['sequence']}:True", "cancelled")
        try:
            if self.store is not None:
                notifier.send(text, event_id=event_id)
            else:
                notifier.send(text)
            self.notified = error is not None
            self.state.update(notified=self.notified, last_notice=now, sequence=self.state["sequence"] + 1)
            if error is None:
                self.state.update(since=None, outage=None)
        except ApiError:
            LOG.error("감시 상태 알림 전송 실패. 다음 확인 때 재시도합니다.")
        finally:
            if self.store is not None:
                with self.store.db:
                    self.store.db.execute("INSERT OR REPLACE INTO health VALUES (?,?)", (self.label, json.dumps(self.state)))


class PollSchedule:
    """Separate fetch backoff per account; normal accounts keep their own poll cadence."""
    def __init__(self, poll):
        self.poll = poll
        self.next = {}
        self.failures = {}

    def due(self, account, now):
        return now >= self.next.get(account, 0)

    def record(self, account, now, error=None):
        failures = self.failures.get(account, 0) + 1 if error else 0
        self.failures[account] = failures
        delay = max(error.retry_after, min(self.poll * 2 ** min(failures, 5), 3600)) if error else self.poll
        self.next[account] = now + delay


def cross_check(name, exc, fetched, failed) -> str:
    """Compare with the other accounts polled in the same round to tell local from X-side faults."""
    others = sorted(n for n in fetched if n != name)
    if others:
        return f"교차 확인: 같은 시각 @{others[0]} 조회는 성공 → 이 PC 인터넷과 봇 설정은 정상, 이 계정 페이지만 실패했습니다."
    if len(failed) > 1:
        if getattr(exc, "side", "") == "network":
            return "교차 확인: 모든 계정 조회 실패 → 이 PC 네트워크 문제일 가능성이 높습니다."
        return "교차 확인: 모든 계정 조회 실패 → X 전체 장애 또는 X의 접근 제한 가능성이 높습니다."
    return "교차 확인: 비교할 다른 계정 결과가 없습니다."


def parse_accounts(spec: str, legacy_username: str = ""):
    """X_ACCOUNTS="thsottiaux:codex,claudedevs:claude"; falls back to X_USERNAME as a Codex account."""
    spec = spec.strip() or f"{legacy_username.strip() or 'thsottiaux'}:codex"
    accounts = []
    for item in filter(None, (part.strip() for part in spec.split(","))):
        name, _, kind = item.partition(":")
        name, kind = name.strip().lstrip("@").lower(), (kind.strip().lower() or "codex")
        if not re.fullmatch(r"[a-z0-9_]{1,15}", name):
            raise ValueError("X_ACCOUNTS에는 URL 대신 계정 이름을 넣으세요. 예: thsottiaux:codex,claudedevs:claude")
        if kind not in PRODUCTS:
            raise ValueError(f"X_ACCOUNTS의 제품 이름은 {', '.join(PRODUCTS)} 중 하나여야 합니다.")
        if name not in {n for n, _ in accounts}:
            accounts.append((name, kind))
    if not accounts:
        raise ValueError(".env의 X_ACCOUNTS 값을 설정하세요.")
    return accounts


def setting(name, default=None):
    value = os.getenv(name, default)
    if value is None or not str(value).strip():
        raise ValueError(f".env의 {name} 값을 설정하세요.")
    return str(value).strip()


def inspect_state(path, zone, history=None):
    """Read-only diagnostics; does not create/migrate a database or require API credentials."""
    if not path.exists():
        print("상태 파일이 없습니다. 아직 감시를 실행하지 않았거나 STATE_PATH를 확인하세요.")
        return
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    def stamp(value):
        return datetime.fromtimestamp(value, UTC).astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M:%S %Z") if value else "없음"
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if history is not None:
            if "outbox" not in tables:
                print("전송 이력이 없습니다. 업데이트 이후의 알림부터 기록됩니다.")
                return
            state_expr = "state" if "state" in {row[1] for row in db.execute("PRAGMA table_info(outbox)")} else "CASE WHEN finished_ts IS NULL THEN 'pending' ELSE 'sent' END"
            rows = db.execute(f"SELECT event_id,text,created_ts,finished_ts,{state_expr} FROM outbox ORDER BY created_ts DESC LIMIT ?", (history,)).fetchall()
            labels = {"pending": "미완료", "sent": "전송 완료", "expired": "기한 만료", "cancelled": "예약 취소"}
            for event_id, text, created, finished, state in rows:
                receipts = db.execute("SELECT COUNT(*) FROM receipts WHERE event_id=?", (event_id,)).fetchone()[0]
                print(f"[{stamp(created)}] {labels[state]} · 성공 조각 {receipts}개 · {event_id}\n{text}\n")
            if not rows:
                print("전송 이력이 없습니다.")
            return
        print(f"상태 DB: {path}\n")
        accounts = [row[0] for row in db.execute("SELECT account FROM web_monitor")] if "web_monitor" in tables else []
        for account in accounts:
            row = db.execute("SELECT state FROM runtime WHERE account=?", (account,)).fetchone() if "runtime" in tables else None
            state = json.loads(row[0]) if row else {}
            pending = db.execute("SELECT COUNT(*) FROM posts p WHERE account=? AND NOT EXISTS (SELECT 1 FROM seen s WHERE s.account=p.account AND s.post_id=p.post_id)", (account,)).fetchone()[0] if "posts" in tables else 0
            retries = db.execute("SELECT COUNT(*) FROM retries WHERE account=?", (account,)).fetchone()[0] if "retries" in tables else 0
            print(f"@{account}\n  마지막 조회 성공: {stamp(state.get('fetch_ok'))}\n  마지막 AI 성공: {stamp(state.get('ai_ok'))}\n  미처리 글: {pending}개 / 재시도 항목: {retries}개\n  웹 오류: {state.get('web_error') or '없음'}\n  처리 오류: {state.get('process_error') or '없음'}\n")
        if "outbox" in tables:
            print("미완료 알림:", db.execute("SELECT COUNT(*) FROM outbox WHERE finished_ts IS NULL").fetchone()[0])
    finally:
        db.close()


def run_monitor(accounts, clients, store, notifier, zone, source_zone, poll, lookback,
                analyzer, max_posts, once=False, dry_run=False, notify_startup=False,
                reminder_minutes=(), retention=90, health_repeat=60):
    cutoffs = {name: store.cutoff(name, datetime.now(UTC) - timedelta(hours=lookback)) for name, _ in accounts}
    schedules = PollSchedule(poll)
    healths = {name: HealthNotice(f"@{name} X 웹 감시", store, health_repeat * 60) for name, _ in accounts}
    ai_healths = {name: HealthNotice(f"@{name} Codex AI 해석", store, health_repeat * 60) for name, _ in accounts}
    startup_health = HealthNotice("Codex 시작 연결 시험", store, health_repeat * 60)
    web_errors, fetched_once = {}, set()
    startup_checked = analyzer is None
    probe_next, prune_next = 0, 0
    startup_id = "startup:" + uuid.uuid4().hex
    while True:
        errors, fetched, web_failed = [], set(), {}
        # Collect every due profile before running potentially slow AI calls.
        for name, _ in accounts:
            if not schedules.due(name, time.monotonic()):
                continue
            try:
                clients[name].fetch(cutoffs[name])
                store.remember(name, clients[name].last_posts)
                fetched.add(name)
                fetched_once.add(name)
                web_errors.pop(name, None)
                schedules.record(name, time.monotonic())
                if not dry_run:
                    store.runtime(name, fetch_ok=time.time(), web_error=None)
            except WebFetchError as exc:
                errors.append(exc)
                web_errors[name] = exc
                web_failed[name] = exc
                schedules.record(name, time.monotonic(), exc)
                if not dry_run:
                    store.runtime(name, web_error=str(exc))
                LOG.error("@%s: %s", name, exc)
        for name, kind in accounts:
            pending = store.pending(name, cutoffs[name], ready_only=True)
            account_errors = []
            if pending:
                try:
                    count = process(pending, name, store, notifier, zone, source_zone, dry_run, analyzer, max_posts, kind, clients[name].thread if name in fetched else None)
                    LOG.info("@%s: 새로 처리 %s개", name, count)
                except (ApiError, AIError, WebFetchError, BatchError) as exc:
                    account_errors.extend(exc.errors if isinstance(exc, BatchError) else [exc])
            if analyzer is not None and not dry_run and name in fetched:
                try:
                    follow_up(name, store, notifier, zone, analyzer, kind, clients[name].thread)
                except (ApiError, AIError, WebFetchError, BatchError) as exc:
                    account_errors.extend(exc.errors if isinstance(exc, BatchError) else [exc])
            errors.extend(account_errors)
            if not dry_run:
                if pending or name in fetched:
                    saved_errors = [json.loads(row[0])["message"] for row in store.db.execute("SELECT error FROM retries WHERE account=? AND phase IN ('process','followup')", (name,))]
                    store.runtime(name, process_error=" / ".join(dict.fromkeys(saved_errors)) or None)
                if not once:
                    web_error = web_errors.get(name)
                    # Compare only profiles attempted in this round; never infer from stale success.
                    detail = f"{web_error}\n{cross_check(name, web_error, fetched, web_failed)}" if name in web_failed else web_error
                    healths[name].update(notifier, detail)
                    ai_healths[name].update(notifier, store.ai_failure(name))
            if name in fetched:
                LOG.info("@%s 웹 확인 완료: 공개 글 %s개 (공개 페이지 범위만 확인)", name, clients[name].last_visible_count)
        # A startup AI/login error no longer prevents profile collection or other work.
        if notify_startup and not startup_checked and time.monotonic() >= probe_next:
            try:
                check = analyzer.analyze({"id": "startup-check", "text": "Connection test only. No reset news.", "created_at": datetime.now(UTC).isoformat()}, [])
                if check.relevant:
                    raise AIError("Codex 연결 시험의 관련 없음 판독이 실패했습니다.")
                startup_checked = True
                startup_health.update(notifier)
            except AIError as exc:
                errors.append(exc)
                probe_next = time.monotonic() + exc.retry_after
                if not once:
                    startup_health.update(notifier, exc)
        if notifier is not None and not dry_run:
            try:
                send_reminders(store, notifier, zone, reminder_minutes)
            except (ApiError, BatchError) as exc:
                errors.extend(exc.errors if isinstance(exc, BatchError) else [exc])
            if notify_startup and startup_checked and len(fetched_once) == len(accounts) and not web_errors and not errors:
                waiting = sum(store.db.execute("SELECT COUNT(*) FROM retries WHERE account=?", (name,)).fetchone()[0] for name, _ in accounts)
                if not waiting:
                    watched = ", ".join(f"@{name}({PRODUCTS[kind][0]})" for name, kind in accounts)
                    try:
                        notifier.send(f"✅ AI 리셋 알림 봇 재시작 성공\n공개 페이지 조회 성공: {watched}\n분석: {'Codex · ChatGPT 로그인 연결 시험 성공' if analyzer else '규칙 기반'}\n확인 주기: {poll // 60}분", event_id=startup_id)
                        notify_startup = False
                    except ApiError as exc:
                        errors.append(exc)
            if time.monotonic() >= prune_next:
                store.prune(retention)
                prune_next = time.monotonic() + 86400
        if once or dry_run:
            waiting = any(store.db.execute("SELECT 1 FROM retries WHERE account=? LIMIT 1", (name,)).fetchone() for name, _ in accounts)
            return 1 if errors or waiting else 0
        # A short scheduler tick also handles reminder deadlines and item-specific retries.
        delay = min(30, max(1, min(schedules.next.values()) - time.monotonic()))
        time.sleep(delay)


def main():
    # Windows redirected output may default to CP949, which cannot encode emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--dry-run", action="store_true", help="read public X HTML once; no notifications or state writes")
    mode.add_argument("--chat-id", action="store_true")
    mode.add_argument("--setup-telegram", action="store_true", help="save the single private chat ID and send a test")
    mode.add_argument("--test-telegram", action="store_true")
    mode.add_argument("--test-discord", action="store_true")
    mode.add_argument("--sample", action="store_true", help="offline preview without credentials")
    mode.add_argument("--preview-ai", action="store_true", help="analyze a visible post with Codex; no notifications or persistent state writes")
    mode.add_argument("--status", action="store_true", help="read local health and pending counts; no network or state writes")
    mode.add_argument("--history", nargs="?", type=int, const=20, help="read the last N notification records (default 20); no network")
    parser.add_argument("--notify-startup", action="store_true", help="notify only after a successful first scan and AI self-check")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        zone = setting("DISPLAY_TIMEZONE", "Asia/Seoul")
        source_zone = setting("SOURCE_TIMEZONE", "America/Los_Angeles")
        ZoneInfo(zone)
        ZoneInfo(source_zone)
        path = Path(setting("STATE_PATH", "data/state.sqlite3"))
        path = path if path.is_absolute() else ROOT / path
        if args.status or args.history is not None:
            if args.history is not None and not 1 <= args.history <= 200:
                raise ValueError("--history 개수는 1~200이어야 합니다.")
            inspect_state(path, zone, args.history)
            return 0
        if args.sample:
            post = {"id": "0", "created_at": "2026-10-18T01:00:00Z", "text": "Codex rate limits will reset in 3 hours."}
            print(message("thsottiaux", post, assess(post["text"], datetime.fromisoformat(post["created_at"])), zone))
            return 0
        telegram = notifier = None
        if args.test_discord:
            Discord(setting("DISCORD_WEBHOOK_URL")).send("✅ 코덱스 리셋 알림 봇 연결 테스트")
            print("디스코드 전송 성공")
            return 0
        if args.chat_id or args.setup_telegram or args.test_telegram:
            telegram = Telegram(setting("TELEGRAM_BOT_TOKEN"), "" if (args.chat_id or args.setup_telegram) else setting("TELEGRAM_CHAT_ID"))
        elif not args.dry_run and not args.preview_ai:
            channels = []
            if os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
                channels.append(Telegram(setting("TELEGRAM_BOT_TOKEN"), setting("TELEGRAM_CHAT_ID")))
            if os.getenv("DISCORD_WEBHOOK_URL", "").strip():
                channels.append(Discord(setting("DISCORD_WEBHOOK_URL")))
            if not channels:
                raise ValueError(".env에 TELEGRAM_BOT_TOKEN 또는 DISCORD_WEBHOOK_URL을 설정하세요.")
            notifier = Notifier(channels)
        if args.chat_id:
            print("Chat IDs:", ", ".join(telegram.chats()) or "없음: 봇에 /start를 보내고 다시 실행하세요. webhook 설정 시 getUpdates 사용 불가.")
            return 0
        if args.setup_telegram:
            chats = telegram.chats()
            if len(chats) != 1:
                raise ValueError("개인 채팅이 1개여야 자동 연결 가능합니다. 만든 봇에 /start를 보내세요. 여러 개면 TELEGRAM_CHAT_ID를 직접 설정하세요.")
            telegram.chat_id = chats[0]
            telegram.send("✅ 코덱스 리셋 알림 봇 연결 테스트\nX 공개 웹페이지 감시를 준비했습니다.")
            set_key(str(ROOT / ".env"), "TELEGRAM_CHAT_ID", chats[0])
            print("텔레그램 연결·채팅 ID 저장 완료")
            return 0
        if args.test_telegram:
            telegram.send("✅ 코덱스 리셋 알림 봇 연결 테스트")
            print("텔레그램 전송 성공")
            return 0
        accounts = parse_accounts(os.getenv("X_ACCOUNTS", ""), os.getenv("X_USERNAME", ""))
        username, product = accounts[0]
        poll = int(setting("POLL_SECONDS", "300"))
        lookback = int(setting("INITIAL_LOOKBACK_HOURS", "24"))
        if poll < 60 or not 1 <= lookback <= 168:
            raise ValueError("POLL_SECONDS >= 60, INITIAL_LOOKBACK_HOURS는 1~168이어야 합니다.")
        clients = {name: WebClient(name) for name, _ in accounts}
        client = clients[username]
        ai_enabled = setting("AI_ENABLED", "true").lower() == "true"
        analyzer = CodexAnalyzer(os.getenv("CODEX_COMMAND", ""), os.getenv("CODEX_MODEL", ""), int(setting("CODEX_TIMEOUT_SECONDS", "120"))) if ai_enabled or args.preview_ai else None
        max_posts = int(setting("AI_MAX_POSTS_PER_POLL", "10"))
        if not 1 <= max_posts <= 50:
            raise ValueError("AI_MAX_POSTS_PER_POLL은 1~50이어야 합니다.")
        reminder_minutes = [int(v.strip()) for v in os.getenv("REMINDER_MINUTES", "").split(",") if v.strip()]
        if any(v < 0 or v > 1440 for v in reminder_minutes):
            raise ValueError("REMINDER_MINUTES는 0~1440의 분 단위 목록이어야 합니다.")
        retention = int(setting("RETENTION_DAYS", "90"))
        health_repeat = int(setting("HEALTH_REPEAT_MINUTES", "60"))
        if retention < 7 or health_repeat < 1:
            raise ValueError("RETENTION_DAYS >= 7, HEALTH_REPEAT_MINUTES >= 1이어야 합니다.")
        if args.preview_ai:
            visible = client.fetch(datetime.now(UTC) - timedelta(days=7))
            if not visible:
                raise ValueError("최근 공개 게시글이 없어 AI 미리보기를 진행하지 못했습니다.")
            post = next((p for p in reversed(visible) if assess(text_of(p), datetime.fromisoformat(p["created_at"])).relevant), visible[-1])
            context = [p for p in visible if int(p["id"]) < int(post["id"])][-8:]
            result = analyzer.analyze(post, context, product)
            print(ai_message(username, post, result, zone, product))
            return 0
        store = Store(":memory:" if args.dry_run else str(path))
        if notifier is not None:
            notifier.store = store
        if not args.dry_run and not args.once:
            log_path = (path if path.is_absolute() else ROOT / path).parent / "bot.log"
            file_log = RotatingFileHandler(log_path, maxBytes=500_000, backupCount=2, encoding="utf-8")
            file_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(file_log)
        return run_monitor(accounts, clients, store, notifier, zone, source_zone, poll, lookback,
                           analyzer, max_posts, args.once, args.dry_run, args.notify_startup,
                           reminder_minutes, retention, health_repeat)
    except KeyboardInterrupt:
        LOG.info("종료")
        return 0
    except (AIError, ApiError, WebFetchError) as exc:
        LOG.error("%s", exc)
        return 1
    except (ValueError, KeyError, sqlite3.Error, OSError) as exc:
        LOG.error("설정/데이터 오류 (%s). .env와 상태 파일을 확인하세요.", type(exc).__name__)
        if isinstance(exc, ValueError):
            LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
