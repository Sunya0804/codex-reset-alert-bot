import tempfile
import unittest
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from bot import ApiError, Discord, cross_check, HealthNotice, Notifier, Store, assess, chunks, message, parse_accounts, process, request_json


class BotTests(unittest.TestCase):
    def setUp(self):
        self.created = datetime(2026, 10, 18, 1, tzinfo=timezone.utc)

    def parse(self, text):
        return assess(text, self.created)

    def test_relative_time_uses_publication_not_poll_time(self):
        self.assertEqual(self.parse('Codex reset in 3 hours').when.hour, 4)
        self.assertEqual(self.parse('reset in 30 minutes').when.minute, 30)

    def test_sample_works_with_windows_legacy_encoding(self):
        env = dict(os.environ, PYTHONIOENCODING='cp949')
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('bot.py')), '--sample'], capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        self.assertIn('2026-10-18 13:00 KST', result.stdout.decode('utf-8'))

    def test_pacific_tomorrow_and_korean_conversion(self):
        result = self.parse('Codex reset tomorrow at 10am PT')
        post = {'id': '1', 'text': 'sample'}
        self.assertIn('2026-10-19 02:00 KST', message('thsottiaux', post, result, 'Asia/Seoul'))

    def test_explicit_date(self):
        self.assertEqual(self.parse('reset 2026-10-20 at 10:30 UTC').when.day, 20)
        self.assertEqual(self.parse('reset 2026-10-20T10:30:00Z').when.hour, 10)

    def test_unknown_does_not_invent_time(self):
        for text in ['Le limit reset', 'reset tomorrow morning', 'reset at 10am', 'reset at 10am PT', 'reset 2026-02-30 at 10am UTC', 'reset today at 25:99 UTC']:
            with self.subTest(text=text):
                self.assertTrue(self.parse(text).relevant)
                self.assertIsNone(self.parse(text).when)

    def test_completed_cancelled_and_tentative(self):
        for text in ['We have reset Codex limits', 'No reset tomorrow at 10am PT', 'Maybe reset in 3 hours', 'Reset in 3 hours?']:
            with self.subTest(text=text):
                self.assertTrue(self.parse(text).relevant)
                self.assertIsNone(self.parse(text).when)

    def test_unrelated(self):
        self.assertFalse(self.parse('Codex is fast').relevant)
        self.assertFalse(self.parse('reset your password').relevant)

    def test_dst_transition_is_uncertain(self):
        self.assertIsNone(self.parse('reset 2026-11-01 at 1:30am PT').when)
        self.assertIsNone(self.parse('reset 2026-03-08 at 2:30am PT').when)

    def test_failure_does_not_skip_unsent_and_restart_keeps_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'state.sqlite3')
            store = Store(path)
            posts = [{'id': str(i), 'text': text, 'created_at': self.created.isoformat()} for i, text in [(1, 'hello'), (2, 'reset in 3 hours')]]
            telegram = Mock()
            telegram.send.side_effect = ApiError('Telegram')
            with self.assertRaises(ApiError):
                process(posts, 'test', store, telegram, 'Asia/Seoul', 'UTC')
            self.assertEqual(store.get('test'), '1')
            telegram.send.side_effect = None
            process(posts[1:], 'test', store, telegram, 'Asia/Seoul', 'UTC')
            store.db.close()
            reopened = Store(path)
            self.assertEqual(reopened.get('test'), '2')
            reopened.db.close()

    def test_dry_run_never_sends_or_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'state.sqlite3'))
            telegram = Mock()
            post = {'id': '1', 'text': 'reset', 'created_at': self.created.isoformat()}
            with patch('builtins.print'):
                process([post], 'test', store, telegram, 'UTC', 'UTC', True)
            telegram.send.assert_not_called()
            self.assertIsNone(store.get('test'))
            store.db.close()

    def test_late_visible_post_and_repeated_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'state.sqlite3'))
            telegram = Mock()
            post = lambda i: {'id': str(i), 'text': 'reset in 3 hours', 'created_at': self.created.isoformat()}
            process([post(5)], 'test', store, telegram, 'UTC', 'UTC')
            self.assertEqual(process([post(5), post(3)], 'test', store, telegram, 'UTC', 'UTC'), 1)
            self.assertEqual(process([post(3), post(5)], 'test', store, telegram, 'UTC', 'UTC'), 0)
            self.assertEqual(telegram.send.call_count, 2)
            self.assertEqual(store.get('test'), '5')
            cutoff = store.cutoff('test', self.created)
            self.assertEqual(store.cutoff('test', self.created.replace(day=20)), cutoff)
            store.db.close()

    def test_outage_notification_and_recovery(self):
        from web_source import WebFetchError
        health, telegram = HealthNotice(), Mock()
        health.update(telegram, WebFetchError('login'))
        health.update(telegram, WebFetchError('login'))
        self.assertEqual(telegram.send.call_count, 1)
        health.update(telegram)
        health.update(telegram)
        self.assertEqual(telegram.send.call_count, 2)

    def test_failed_health_notice_retries(self):
        from web_source import WebFetchError
        health, telegram = HealthNotice(), Mock()
        telegram.send.side_effect = ApiError('Telegram')
        health.update(telegram, WebFetchError('login'))
        self.assertFalse(health.notified)
        telegram.send.side_effect = None
        health.update(telegram, WebFetchError('login'))
        self.assertTrue(health.notified)

    @patch('bot.HTTP.request')
    def test_errors_never_leak_token(self, request):
        import requests
        request.side_effect = requests.ConnectionError('https://api.telegram.org/botSECRET/sendMessage')
        with self.assertRaises(ApiError) as caught:
            request_json('Telegram', 'POST', 'https://api.telegram.org/botSECRET/sendMessage')
        self.assertNotIn('SECRET', str(caught.exception))

    @patch('bot.HTTP.request')
    def test_rate_limit_and_partial_api_errors(self, request):
        request.return_value = Mock(ok=False, status_code=429, headers={'Retry-After': '120'})
        with self.assertRaises(ApiError) as caught:
            request_json('X', 'GET', 'https://api.x.com')
        self.assertEqual(caught.exception.retry_after, 120)
        request.return_value = Mock(ok=True)
        request.return_value.json.return_value = {'data': [], 'errors': [{'detail': 'partial failure'}]}
        with self.assertRaises(ApiError):
            request_json('X', 'GET', 'https://api.x.com')

    def test_discord_chunks_respect_limit(self):
        text = '🔔 제목\n' + ('가' * 2500) + '\n\n' + '\n'.join('줄 %d' % i for i in range(400))
        parts = chunks(text)
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 1900 for p in parts))
        self.assertEqual(''.join(parts).replace('\n', ''), text.replace('\n', ''))

    @patch('bot.HTTP.request')
    def test_discord_payload_blocks_mentions(self, request):
        request.return_value = Mock(ok=True)
        request.return_value.json.return_value = {'id': '1'}
        Discord('https://discord.com/api/webhooks/1/abc-DEF_9').send('@everyone hi')
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs['json']['allowed_mentions'], {'parse': []})
        self.assertEqual(kwargs['params'], {'wait': 'true'})
        with self.assertRaises(ValueError):
            Discord('https://example.com/api/webhooks/1/abc')

    def test_cross_check_separates_local_and_x_faults(self):
        from web_source import WebFetchError
        x_err, net_err = WebFetchError('a', side='x'), WebFetchError('b', side='network')
        self.assertIn('@thsottiaux 조회는 성공', cross_check('claudedevs', x_err, {'thsottiaux'}, {'claudedevs': x_err}))
        self.assertIn('네트워크', cross_check('a', net_err, set(), {'a': net_err, 'b': net_err}))
        self.assertIn('X 전체 장애', cross_check('a', x_err, set(), {'a': x_err, 'b': x_err}))

    def test_parse_accounts(self):
        self.assertEqual(parse_accounts(' @thsottiaux:codex, ClaudeDevs:claude ,'), [('thsottiaux', 'codex'), ('claudedevs', 'claude')])
        self.assertEqual(parse_accounts('', 'someone'), [('someone', 'codex')])
        self.assertEqual(parse_accounts(''), [('thsottiaux', 'codex')])
        for bad in ('https://x.com/a:codex', 'claudedevs:gemini'):
            with self.assertRaises(ValueError):
                parse_accounts(bad)

    def test_product_label_in_message(self):
        post = {'id': '1', 'created_at': '2026-10-18T01:00:00+00:00', 'text': 'We reset limits'}
        self.assertIn('Claude 리셋', message('claudedevs', post, assess(post['text'], self.created), 'UTC', 'claude'))

    def test_notifier_retries_only_failed_channel(self):
        ok, flaky = Mock(), Mock()
        flaky.send.side_effect = ApiError('Discord')
        notifier = Notifier([ok, flaky])
        with self.assertRaises(ApiError):
            notifier.send('hello')
        flaky.send.side_effect = None
        notifier.send('hello')
        self.assertEqual((ok.send.call_count, flaky.send.call_count), (1, 2))
        notifier.send('hello')  # a later identical notice is delivered again
        self.assertEqual(ok.send.call_count, 2)


if __name__ == '__main__':
    unittest.main()
