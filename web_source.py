"""Read only public X profile HTML. No API keys, guest tokens or private endpoints."""
from __future__ import annotations

import re
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

UTC = timezone.utc
HTTP = requests.Session()


def reply_parent(article, post_id, own):
    """Accept explicit parent metadata only; proximity/chronology proves nothing."""
    for node in [article] + list(filter(own, article.select("[data-in-reply-to-status-id], [data-in-reply-to-id]"))):
        value = node.get("data-in-reply-to-status-id") or node.get("data-in-reply-to-id")
        if value and re.fullmatch(r"\d+", value):
            return value
    def visit(value):
        if isinstance(value, dict):
            identity = value.get("id_str") or value.get("rest_id") or value.get("id")
            parent = value.get("in_reply_to_status_id_str") or value.get("in_reply_to_status_id")
            if str(identity) == post_id and parent is not None and re.fullmatch(r"\d+", str(parent)):
                return str(parent)
            for child in value.values():
                found = visit(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = visit(child)
                if found:
                    return found
        return None
    for script in filter(own, article.find_all("script", type="application/json")):
        try:
            parent = visit(json.loads(script.string or ""))
        except (ValueError, RecursionError):
            continue
        if parent:
            return parent
    return None


# Where the fault most likely lies, shown to the user with every failure.
SIDES = {"x": "X 서버 문제 (봇 문제 아님)", "access": "X 접근 제한", "network": "이 PC/서버 네트워크 문제", "bot": "봇 쪽 문제 (설정·코드 확인 필요)"}

# status -> (meaning, likely cause, side)
HTTP_HINTS = {
    400: ("잘못된 요청", "X가 봇의 요청 형식을 거부했습니다. X 쪽 변경이나 봇 요청 헤더 문제일 수 있습니다.", "bot"),
    401: ("인증 필요", "X가 비로그인 열람을 막았습니다.", "access"),
    403: ("접근 거부", "X가 이 IP나 자동 접속을 차단했습니다. 잠시 후 풀리는 경우가 많습니다.", "access"),
    404: ("페이지 없음", "계정 이름이 바뀌었거나 삭제·정지됐을 수 있습니다. X_ACCOUNTS의 계정 이름을 확인하세요.", "bot"),
    429: ("요청 과다", "X의 속도 제한에 걸렸습니다. 확인 주기를 늘리면 줄어듭니다.", "access"),
    500: ("X 서버 내부 오류", "X 서버 쪽 일시 장애입니다. 보통 몇 분 안에 저절로 복구됩니다.", "x"),
    502: ("게이트웨이 오류", "X 서버 간 연결 장애입니다. 보통 곧 복구됩니다.", "x"),
    503: ("서비스 이용 불가", "X 서버 과부하 또는 점검 중입니다.", "x"),
    504: ("게이트웨이 시간 초과", "X 내부 서버가 제때 응답하지 않았습니다. X 과부하 가능성이 높습니다.", "x"),
}


class WebFetchError(RuntimeError):
    def __init__(self, detail: str, retry_after: int = 300, cause: str = "", side: str = ""):
        lines = ["X 웹 감시 실패: " + detail]
        if cause:
            lines.append("추정 원인: " + cause)
        if side:
            lines.append("판단: " + SIDES[side])
        super().__init__("\n".join(lines))
        self.retry_after = retry_after
        self.side = side


def http_error(status: int, retry_after: int) -> WebFetchError:
    meaning, cause, side = HTTP_HINTS.get(status) or (
        ("X 서버 오류", "X 서버 쪽 장애입니다.", "x") if status >= 500 else ("요청 거부", "X가 요청을 거부했습니다. 접근 제한 가능성이 있습니다.", "access"))
    return WebFetchError(f"HTTP {status} ({meaning})", retry_after, cause, side)


def network_error(exc: requests.RequestException) -> WebFetchError:
    # Order matters: SSLError and ConnectTimeout are also ConnectionErrors.
    if isinstance(exc, requests.exceptions.SSLError):
        return WebFetchError("보안 연결(SSL) 실패", cause="백신·프록시가 HTTPS를 가로채거나 PC 시계가 틀렸을 수 있습니다.", side="network")
    if isinstance(exc, requests.Timeout):
        return WebFetchError("응답 시간 초과", cause="X 응답이 매우 느리거나 이 PC의 인터넷이 불안정합니다.", side="network")
    if isinstance(exc, requests.ConnectionError):
        text = str(exc)
        if any(key in text for key in ("NameResolution", "getaddrinfo", "Name or service not known", "nodename nor servname")):
            return WebFetchError("주소(DNS) 조회 실패", cause="이 PC의 인터넷 연결이 끊겼거나 DNS가 동작하지 않습니다.", side="network")
        return WebFetchError("연결 실패", cause="이 PC의 인터넷이 끊겼거나 X 서버에 연결할 수 없습니다.", side="network")
    return WebFetchError("요청 오류", cause="예상하지 못한 요청 오류입니다.", side="bot")


def permalink(href: str):
    parsed = urlparse(href)
    if parsed.netloc and parsed.netloc.lower() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return None
    return re.fullmatch(r"/([A-Za-z0-9_]{1,15})/status/(\d+)(?:/)?", parsed.path)


def post_time(article, post_id: str, own=lambda node: True) -> datetime:
    stamp = next((node for node in article.select("time[datetime]") if own(node)), None)
    if stamp:
        value = datetime.fromisoformat(stamp["datetime"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("timestamp lacks timezone")
        return value.astimezone(UTC)
    # Current server-rendered X HTML attaches this numeric timestamp to its time link.
    for script in filter(own, article.find_all("script")):
        match = re.search(r'"timestamp"\s*:\s*(\d{13})\b', script.string or "")
        if match:
            return datetime.fromtimestamp(int(match[1]) / 1000, UTC)
    # X Snowflake IDs contain the original creation time, independent of poll time.
    millis = (int(post_id) >> 22) + 1288834974657
    if int(post_id) < 1 << 22:
        raise ValueError("invalid post id")
    return datetime.fromtimestamp(millis / 1000, UTC)


def parse_profile(html: str, username: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    posts = {}
    for article in soup.select('article, [data-testid="tweet"]'):
        # A quoted post is nested inside the outer article; it is not a post of this timeline.
        if article.find_parent("article") is not None:
            continue
        nested = article.select("article")
        # Identity check: bs4 compares tags by content, so equal-looking quote links would match.
        own = lambda node: (parent := node.find_parent("article")) is None or parent is article
        # The first profile link is the author (avatar); a quote card may also link the quoted post
        # outside the nested article, so take the author's own first permalink as this post's ID.
        hrefs = [urlparse(link.get("href", "")) for link in article.select("a[href]") if own(link)]
        author = next((h.path[1:] for h in hrefs if not h.netloc and re.fullmatch(r"/[A-Za-z0-9_]{1,15}", h.path)), "")
        if author.lower() != username.lower():
            continue
        quoted = {m[2] for quote in nested for m in (permalink(a.get("href", "")) for a in quote.select("a[href]")) if m}
        match = next((m for m in map(permalink, (h.geturl() for h in hrefs)) if m and m[1].lower() == username.lower() and m[2] not in quoted), None)
        if match is None:
            continue
        if article.select_one('[data-testid="socialContext"]'):
            # Reposts are excluded, including the target's own old posts being reposted.
            continue
        text_node = next((node for node in article.select('[data-testid="tweetText"], div[dir="auto"].whitespace-pre-wrap') if own(node)), None)
        if text_node is None:
            # Media-only posts are valid, but an unrecognized text layout is an error.
            if article.select_one('img[src*="pbs.twimg.com/media/"], video'):
                text = ""
            else:
                raise WebFetchError("본문을 읽지 못함", cause="X 페이지 구조가 바뀌어 봇의 페이지 해석기를 고쳐야 할 수 있습니다. 일시적인 미로딩일 수도 있습니다.", side="bot")
        else:
            for image in text_node.select("img[alt]"):
                image.replace_with(image["alt"])
            text = text_node.get_text("", strip=False).strip()
        try:
            created = post_time(article, match[2], own)
        except (ValueError, OverflowError, OSError):
            raise WebFetchError("게시 시각을 읽지 못함", cause="X 페이지 구조가 바뀌었을 수 있습니다.", side="bot") from None
        posts[match[2]] = {"id": match[2], "text": text, "created_at": created.isoformat()}
        parent = reply_parent(article, match[2], own)
        if parent:
            posts[match[2]]["in_reply_to_id"] = parent
    if not posts:
        raise WebFetchError("공개 게시글을 찾지 못함", cause="X가 로그인을 요구하거나(접근 제한) 페이지 구조가 바뀌었을 수 있습니다.", side="access")
    return sorted(posts.values(), key=lambda post: int(post["id"]))


class WebClient:
    def __init__(self, username: str):
        self.username = username
        self.last_visible_count = 0
        self.last_posts = []
        self.thread_cache = {}

    def get(self, path: str) -> str:
        try:
            response = HTTP.get(
                f"https://x.com{path}",
                headers={"User-Agent": "CodexResetAlertBot/1.0 (public profile monitor)", "Cache-Control": "no-cache", "Accept-Language": "en-US,en;q=0.9"},
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise network_error(exc) from None
        if not response.ok:
            try:
                wait = min(max(int(response.headers.get("Retry-After", "300")), 300), 86400)
            except ValueError:
                wait = 300
            raise http_error(response.status_code, wait)
        final = urlparse(response.url)
        if final.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} or final.path.rstrip("/").lower() != path.lower():
            raise WebFetchError("로그인/다른 페이지로 이동", cause="X가 비로그인 열람을 막았거나 계정 주소가 바뀌었습니다.", side="access")
        return response.text

    def fetch(self, cutoff: datetime) -> list[dict]:
        posts = parse_profile(self.get("/" + self.username), self.username)
        self.last_posts = posts
        self.last_visible_count = len(posts)
        return [p for p in posts if datetime.fromisoformat(p["created_at"]) >= cutoff]

    def thread(self, post_id: str) -> tuple[dict, list[dict]]:
        """Open the post page: the profile cuts long posts at 'Show more', and the author's own
        follow-up replies (often carrying the actual time) appear only here."""
        cached = self.thread_cache.get(post_id)
        if cached and time.monotonic() - cached[0] < 45:
            return cached[1]
        posts = parse_profile(self.get(f"/{self.username}/status/{post_id}"), self.username)
        full = next((p for p in posts if p["id"] == post_id), None)
        if full is None:
            raise WebFetchError("글 상세 페이지에서 원문을 찾지 못함", cause="글이 삭제됐거나 X 페이지 구조가 바뀌었을 수 있습니다.", side="bot")
        linked = {post_id}
        for _ in posts:
            linked.update(p["id"] for p in posts if p.get("in_reply_to_id") in linked)
        replies = [p for p in posts if p["id"] != post_id and p["id"] in linked]
        result = full, replies
        now = time.monotonic()
        self.thread_cache = {key: value for key, value in self.thread_cache.items() if now - value[0] < 45}
        self.thread_cache[post_id] = now, result
        return result
