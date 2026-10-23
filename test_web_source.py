import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from web_source import WebClient, WebFetchError, parse_profile


def article(post_id='2111492673890824348', author='thsottiaux', body='Day 13/ Le limit reset', stamp=True):
    return f'''<article><a href="/{author}">{author}</a>
    <a href="/{author}/status/{post_id}">22h</a>
    {'<script>formatRelativePastTimestamp({"timestamp":1792254057000})</script>' if stamp else ''}
    <div class="whitespace-pre-wrap" dir="auto">{body}</div></article>'''


class WebTests(unittest.TestCase):
    def test_server_rendered_page(self):
        posts = parse_profile(article(), 'thsottiaux')
        self.assertEqual(posts[0]['text'], 'Day 13/ Le limit reset')
        self.assertEqual(datetime.fromisoformat(posts[0]['created_at']).timestamp(), 1792254057)

    def test_time_element_and_emoji(self):
        html = article(body='reset <img alt="🎉"> in 3 hours').replace('<a href="/thsottiaux/status/2111492673890824348">22h</a>', '<a href="/thsottiaux/status/2111492673890824348"><time datetime="2026-10-18T01:00:00Z">date</time></a>')
        post = parse_profile(html, 'thsottiaux')[0]
        self.assertEqual(post['text'], 'reset 🎉 in 3 hours')
        self.assertEqual(datetime.fromisoformat(post['created_at']).hour, 1)

    def test_snowflake_timestamp_fallback(self):
        post = parse_profile(article(stamp=False), 'thsottiaux')[0]
        expected = ((int(post['id']) >> 22) + 1288834974657) / 1000
        self.assertEqual(datetime.fromisoformat(post['created_at']).timestamp(), expected)

    def test_foreign_author_quoting_target_is_ignored(self):
        foreign = article(author='someone', body='<a href="/thsottiaux/status/2111492673890824348">reset</a>')
        self.assertEqual(len(parse_profile(foreign + article(), 'thsottiaux')), 1)

    def test_duplicates_and_order(self):
        posts = parse_profile(article('2111492673890824349') + article() + article(), 'thsottiaux')
        self.assertEqual([p['id'] for p in posts], ['2111492673890824348', '2111492673890824349'])

    def test_login_challenge_and_changed_layout_fail(self):
        for html in ['<h1>Log in to X</h1>', '<div>Just a moment</div>', article().replace('whitespace-pre-wrap', 'new-layout'), article(author='other')]:
            with self.subTest(html=html):
                with self.assertRaises(WebFetchError):
                    parse_profile(html, 'thsottiaux')

    def test_repost_is_excluded(self):
        repost = article('2111492673890824349').replace('<article>', '<article><span data-testid="socialContext">Reposted</span>')
        posts = parse_profile(repost + article(), 'thsottiaux')
        self.assertEqual(len(posts), 1)

    @patch('web_source.HTTP.get')
    def test_fetch_filters_cutoff_and_calls_only_public_profile(self, get):
        get.return_value = Mock(ok=True, url='https://x.com/thsottiaux', text=article())
        client = WebClient('thsottiaux')
        self.assertEqual(client.fetch(datetime(2026, 10, 19, tzinfo=timezone.utc)), [])
        self.assertEqual(client.last_visible_count, 1)
        self.assertEqual(get.call_args.args[0], 'https://x.com/thsottiaux')
        self.assertNotIn('Authorization', get.call_args.kwargs['headers'])

    @patch('web_source.HTTP.get')
    def test_redirect_and_rate_limit(self, get):
        get.return_value = Mock(ok=True, url='https://x.com/i/flow/login', text=article())
        with self.assertRaises(WebFetchError):
            WebClient('thsottiaux').fetch(datetime.now(timezone.utc))
        get.return_value = Mock(ok=False, status_code=429, headers={'Retry-After': '900'})
        with self.assertRaises(WebFetchError) as caught:
            WebClient('thsottiaux').fetch(datetime.now(timezone.utc))
        self.assertEqual(caught.exception.retry_after, 900)

    @patch('web_source.HTTP.get')
    def test_thread_returns_full_text_and_own_replies_only(self, get):
        main = article(body='long text and the rest after show more')
        own = article('2111492673890824400', body='Lands tomorrow before 10am PST.').replace('<article>', '<article data-in-reply-to-status-id="2111492673890824348">')
        other = article('2111492673890824500', author='someone', body='banked reset?')
        get.return_value = Mock(ok=True, url='https://x.com/thsottiaux/status/2111492673890824348', text=main + own + other)
        full, replies = WebClient('thsottiaux').thread('2111492673890824348')
        self.assertEqual(full['text'], 'long text and the rest after show more')
        self.assertEqual([(r['text'], r['in_reply_to_id']) for r in replies], [('Lands tomorrow before 10am PST.', '2111492673890824348')])
        self.assertEqual(get.call_args.args[0], 'https://x.com/thsottiaux/status/2111492673890824348')

    def test_quote_post_keeps_outer_link_text_and_time(self):
        quoted = article('2111000000000000000', author='OpenAIDevs', body='quoted text').replace('1792254057000', '1792000000000')
        outer = article(body='my comment').replace('<a href="/thsottiaux">thsottiaux</a>', '<a href="/thsottiaux">thsottiaux</a>' + quoted, 1)
        # Mimic the status page: the quote (with its own permalink, text and time) precedes the outer permalink.
        posts = parse_profile(outer, 'thsottiaux')
        self.assertEqual([(p['id'], p['text']) for p in posts], [('2111492673890824348', 'my comment')])
        self.assertEqual(datetime.fromisoformat(posts[0]['created_at']).timestamp(), 1792254057)
        # Quoting one's own older post; the quote card link sits outside the nested article.
        mine = article('2111000000000000000', body='old post')
        outer = article(body='new post').replace('<a href="/thsottiaux">thsottiaux</a>', '<a href="/thsottiaux">thsottiaux</a><a href="/thsottiaux/status/2111000000000000000">card</a>' + mine, 1)
        self.assertEqual([(p['id'], p['text']) for p in parse_profile(outer, 'thsottiaux')], [('2111492673890824348', 'new post')])

    @patch('web_source.HTTP.get')
    def test_errors_explain_cause_and_side(self, get):
        cases = [(500, 'X 서버 내부 오류', 'x'), (404, '페이지 없음', 'bot'), (403, '접근 거부', 'access'), (520, 'X 서버 오류', 'x')]
        for status, meaning, side in cases:
            get.return_value = Mock(ok=False, status_code=status, headers={})
            with self.assertRaises(WebFetchError) as caught:
                WebClient('thsottiaux').fetch(datetime.now(timezone.utc))
            self.assertIn(meaning, str(caught.exception))
            self.assertIn('추정 원인', str(caught.exception))
            self.assertEqual(caught.exception.side, side)
        import requests
        for exc, side_text in [(requests.ConnectTimeout('x'), '응답 시간 초과'), (requests.ConnectionError('NameResolutionError'), 'DNS'), (requests.exceptions.SSLError('x'), 'SSL')]:
            get.side_effect = exc
            with self.assertRaises(WebFetchError) as caught:
                WebClient('thsottiaux').fetch(datetime.now(timezone.utc))
            self.assertIn(side_text, str(caught.exception))
            self.assertEqual(caught.exception.side, 'network')


if __name__ == '__main__':
    unittest.main()
