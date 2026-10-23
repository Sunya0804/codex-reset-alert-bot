"""Offline regressions for evidence, queue isolation, durable delivery and scheduling."""
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import bot
from bot import (ApiError, Assessment, Discord, HealthNotice, Notifier, PollSchedule,
                 Store, Telegram, follow_up, inspect_state, process, run_monitor,
                 send_reminders)
from ai_interpreter import AIError, validate
from time_rules import explicit_time, relative_minutes
from web_source import WebClient, WebFetchError
from test_ai_interpreter import post, result
from test_web_source import article

UTC = timezone.utc
BASE = datetime(2026, 10, 19, 1, tzinfo=UTC)


def unknown(target, **changes):
    data = result(target, time_kind='unknown', time_expression='', time_source_post_id=None,
                  relative_minutes=None, scheduled_at=None)
    data.update(changes)
    return validate(data, target, [])


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.db.close)

    def disk_path(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Path(temp.name) / 'state.sqlite3'

    def test_unlinked_later_author_post_is_not_a_reply(self):
        main_id = '2111492673890824348'
        html = article(main_id) + article('2111492673890824400', body='Unrelated meeting tomorrow at 10am PST')
        with patch('web_source.HTTP.get', return_value=Mock(ok=True, url=f'https://x.com/thsottiaux/status/{main_id}', text=html)):
            _, replies = WebClient('thsottiaux').thread(main_id)
        self.assertEqual(replies, [])

    def test_verified_reply_chain_and_short_cache(self):
        root, first, second = '2111492673890824348', '2111492673890824400', '2111492673890824500'
        html = article(root) + article(first).replace('<article>', f'<article data-in-reply-to-status-id="{root}">')
        html += article(second).replace('<article>', f'<article data-in-reply-to-id="{first}">')
        with patch('web_source.HTTP.get', return_value=Mock(ok=True, url=f'https://x.com/thsottiaux/status/{root}', text=html)) as get, patch('web_source.time.monotonic', return_value=100) as clock:
            client = WebClient('thsottiaux')
            _, replies = client.thread(root)
            self.assertEqual([r['in_reply_to_id'] for r in replies], [root, first])
            client.thread(root)
            self.assertEqual(get.call_count, 1)
            clock.return_value = 146
            client.thread(root)
            self.assertEqual(get.call_count, 2)

    def test_json_parent_requires_matching_post_identity(self):
        root, child = '2111492673890824348', '2111492673890824400'
        def html(identity):
            metadata = json.dumps({'id_str': identity, 'in_reply_to_status_id_str': root})
            return article(root) + article(child).replace('</article>', f'<script type="application/json">{metadata}</script></article>')
        with patch.object(WebClient, 'get', return_value=html('999')):
            self.assertEqual(WebClient('thsottiaux').thread(root)[1], [])
        with patch.object(WebClient, 'get', return_value=html(child)):
            self.assertEqual(WebClient('thsottiaux').thread(root)[1][0]['in_reply_to_id'], root)

    def test_reply_array_does_not_establish_relationship(self):
        target = post('Reset day', '200')
        reply = post('in three hours', '201')
        data = result(target, time_source_post_id='201', evidence=[{'post_id': '200', 'quote': 'Reset day'}, {'post_id': '201', 'quote': reply['text']}])
        with self.assertRaises(AIError):
            validate(data, dict(target, replies=[reply]), [])

    def test_duration_must_match_quote(self):
        with self.assertRaises(AIError):
            validate(result(relative_minutes=1), post(), [])
        self.assertEqual(relative_minutes('in three hours'), 180)
        self.assertEqual(relative_minutes('in 1 hour and 30 minutes'), 90)
        self.assertEqual(relative_minutes('in 1.5 hours'), 90)
        for phrase in ('three hours', 'in -3 hours', 'in 3 hours or 5 hours', 'in 1.1 minutes'):
            self.assertIsNone(relative_minutes(phrase))

    def test_explicit_time_must_match_source_date_and_timezone(self):
        target = post('Reset tomorrow at 10am PT')
        data = result(target, time_kind='explicit', time_expression='tomorrow at 10am PT', relative_minutes=None, scheduled_at='2035-01-01T10:00:00+09:00')
        with self.assertRaises(AIError):
            validate(data, target, [])
        data['scheduled_at'] = '2026-10-19T17:00:00Z'
        self.assertEqual(validate(data, target, []).when, datetime(2026, 10, 19, 17, tzinfo=UTC))

    def test_dst_and_conflicting_dates_are_unconfirmed(self):
        for phrase in ('2026-11-01 at 1:30am PT', '2026-03-08 at 2:30am PT', 'tomorrow 2026-10-20 at 10am PT', '10am PT', 'tomorrow at 10am PT or 11am PT'):
            self.assertIsNone(explicit_time(phrase, BASE), phrase)

    def test_cache_includes_context_and_instruction_contents(self):
        target = post()
        analyzer = Mock(model='', analyze=Mock(return_value=validate(result(), target, [])))
        self.store.analysis('test', target, analyzer, [])
        self.store.analysis('test', target, analyzer, [])
        self.assertEqual(analyzer.analyze.call_count, 1)
        self.store.analysis('test', target, analyzer, [post('Earlier context', '122')])
        self.assertEqual(analyzer.analyze.call_count, 2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('ai_schema.json', 'ai_instructions.txt'):
                (root / name).write_bytes((bot.ROOT / name).read_bytes())
            with patch('bot.ROOT', root):
                self.store.analysis('test', target, analyzer, [])
                instructions = root / 'ai_instructions.txt'
                instructions.write_text(instructions.read_text(encoding='utf-8') + '\nChanged.', encoding='utf-8')
                self.store.analysis('test', target, analyzer, [])
        self.assertEqual(analyzer.analyze.call_count, 4)

    def test_invalid_cache_is_reanalyzed(self):
        target, analyzer = post(), Mock(model='')
        analyzer.analyze.return_value = validate(result(), target, [])
        self.store.analysis('test', target, analyzer, [])
        with self.store.db:
            self.store.db.execute("UPDATE analyses SET result='{}'")
        self.store.analysis('test', target, analyzer, [])
        self.assertEqual(analyzer.analyze.call_count, 2)

    def test_failed_post_does_not_block_later_announcement(self):
        bad, good = post(post_id='123'), post(post_id='124')
        analyzer = Mock(model='')
        analyzer.analyze.side_effect = lambda target, *_: (_ for _ in ()).throw(AIError('bad result')) if target['id'] == '123' else validate(result(target), target, [])
        self.store.remember('test', [bad, good])
        notifier = Mock()
        with self.assertRaises(AIError):
            process([bad, good], 'test', self.store, notifier, 'UTC', 'UTC', analyzer=analyzer)
        self.assertFalse(self.store.has_seen('test', '123'))
        self.assertTrue(self.store.has_seen('test', '124'))
        notifier.send.assert_called_once()

    def test_attempt_budget_counts_failures(self):
        analyzer = Mock(model='', analyze=Mock(side_effect=AIError('bad result')))
        with self.assertRaises(AIError):
            process([post(post_id='123'), post(post_id='124')], 'test', self.store, Mock(), 'UTC', 'UTC', analyzer=analyzer, max_posts=1)
        self.assertEqual(analyzer.analyze.call_count, 1)

    def test_retry_backoff_survives_restart_without_losing_pending(self):
        path = self.disk_path()
        store = Store(str(path))
        target = post()
        store.remember('test', [target])
        store.failed('test', '123', 'process', AIError('timeout'), now=1000)
        store.db.close()
        store = Store(str(path))
        try:
            with patch('bot.time.time', return_value=1001):
                self.assertEqual(store.pending('test', BASE - timedelta(days=1), ready_only=True), [])
                self.assertEqual(len(store.pending('test', BASE - timedelta(days=1))), 1)
            with patch('bot.time.time', return_value=1301):
                self.assertEqual(len(store.pending('test', BASE - timedelta(days=1), ready_only=True)), 1)
        finally:
            store.db.close()

    def test_discord_partial_delivery_resumes_after_restart(self):
        path = self.disk_path()
        store = Store(str(path))
        text = 'a' * 2500
        channel = Discord('https://discord.com/api/webhooks/1/abc')
        with patch('bot.request_json', side_effect=[{}, ApiError('Discord')]) as request:
            with self.assertRaises(ApiError):
                Notifier([channel], store).send(text, event_id='notice')
            first = request.call_args_list[0].kwargs['json']['content']
        store.db.close()
        store = Store(str(path))
        try:
            with patch('bot.request_json', return_value={}) as request:
                # Frozen text and destination identity survive a new Notifier and changed input.
                Notifier([Discord(channel.url)], store).send('changed text', event_id='notice')
                self.assertEqual(request.call_count, 1)
                self.assertNotEqual(request.call_args.kwargs['json']['content'], first)
                self.assertTrue(store.event('notice')['finished'])
                Notifier([Discord(channel.url)], store).send(text, event_id='notice')
                self.assertEqual(request.call_count, 1)
        finally:
            store.db.close()

    def test_channel_order_change_does_not_resend_success(self):
        telegram = Telegram('secret', '1')
        discord = Discord('https://discord.com/api/webhooks/1/abc')
        with patch.object(telegram, 'send') as tg, patch.object(discord, 'send_part', side_effect=ApiError('Discord')) as dc:
            with self.assertRaises(ApiError):
                Notifier([telegram, discord], self.store).send('notice', event_id='x')
            dc.side_effect = None
            Notifier([discord, telegram], self.store).send('notice', event_id='x')
            self.assertEqual(tg.call_count, 1)
            self.assertEqual(dc.call_count, 2)

    def test_crash_before_seen_commit_does_not_resend_or_reanalyze(self):
        target, analyzer = post(), Mock(model='')
        analyzer.analyze.return_value = validate(result(), target, [])
        channel = Telegram('secret', '1')
        notifier = Notifier([channel], self.store)
        with patch.object(channel, 'send') as send:
            with patch.object(self.store, 'advance', side_effect=sqlite3.OperationalError('crash')):
                with self.assertRaises(sqlite3.OperationalError):
                    process([target], 'test', self.store, notifier, 'UTC', 'UTC', analyzer=analyzer)
            process([target], 'test', self.store, Notifier([channel], self.store), 'UTC', 'UTC', analyzer=analyzer)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(analyzer.analyze.call_count, 1)
        self.assertTrue(self.store.has_seen('test', target['id']))

    def test_known_time_keeps_watch_and_cancellation_clears_schedule(self):
        target = post()
        analyzer = Mock(model='', analyze=Mock(return_value=validate(result(), target, [])))
        notifier = Mock()
        thread = Mock(return_value=(target, []))
        process([target], 'test', self.store, notifier, 'UTC', 'UTC', analyzer=analyzer, thread=thread)
        self.assertEqual(len(self.store.watched('test', BASE)), 1)
        reply = dict(post('Reset cancelled.', '124'), in_reply_to_id=target['id'])
        thread.return_value = target, [reply]
        analyzer.analyze.side_effect = lambda t, *_: unknown(t, status='cancelled', evidence=[{'post_id': target['id'], 'quote': target['text']}, {'post_id': reply['id'], 'quote': reply['text']}])
        follow_up('test', self.store, notifier, 'UTC', analyzer, 'codex', thread, BASE + timedelta(minutes=1))
        self.assertEqual(self.store.watched('test', BASE), [])
        self.assertEqual(self.store.db.execute('SELECT active FROM schedules').fetchone()[0], 0)

    def test_followup_retry_uses_frozen_update(self):
        target = post('Reset day', '200')
        reply = dict(post('Still waiting', '201'), in_reply_to_id='200')
        self.store.watch('test', target, BASE + timedelta(hours=6))
        analyzer = Mock(model='', analyze=Mock(side_effect=lambda t, *_: unknown(t)))
        thread = Mock(return_value=(target, [reply]))
        notifier = Mock()
        notifier.send.side_effect = ApiError('Telegram')
        with self.assertRaises(ApiError):
            follow_up('test', self.store, notifier, 'UTC', analyzer, 'codex', thread, BASE)
        thread.reset_mock()
        notifier.send.side_effect = None
        follow_up('test', self.store, notifier, 'UTC', analyzer, 'codex', thread, BASE + timedelta(minutes=3))
        thread.assert_not_called()
        self.assertEqual(analyzer.analyze.call_count, 1)

    def test_failed_followup_is_not_discarded_at_watch_expiry(self):
        target = post()
        self.store.watch('test', target, BASE)
        self.store.failed('test', target['id'], 'followup', AIError('timeout'), now=BASE.timestamp())
        self.assertEqual(len(self.store.watched('test', BASE + timedelta(hours=1))), 1)

    def test_unrelated_reply_does_not_cancel_parent_schedule(self):
        target = post()
        self.store.update_schedule('test', 'codex', target, validate(result(), target, []))
        reply = dict(post('Lunch', '124'), in_reply_to_id='123')
        self.store.update_schedule('test', 'codex', reply, unknown(reply, relevant=False, status='unrelated', evidence=[]))
        self.assertEqual(self.store.db.execute('SELECT active FROM schedules').fetchone()[0], 1)

    def test_reminders_are_once_per_schedule_revision(self):
        target, when = post(), BASE + timedelta(hours=3)
        self.store.update_schedule('test', 'codex', target, validate(result(), target, []))
        channel = Telegram('secret', '1')
        notifier = Notifier([channel], self.store)
        with patch.object(channel, 'send') as send:
            self.assertEqual(send_reminders(self.store, notifier, 'UTC', [10, 0], when - timedelta(minutes=10)), 1)
            self.assertEqual(send_reminders(self.store, notifier, 'UTC', [10, 0], when - timedelta(minutes=10)), 0)
            self.assertEqual(send_reminders(self.store, notifier, 'UTC', [10, 0], when), 1)
            self.assertEqual(send.call_count, 2)

    def test_cancelled_or_changed_schedule_cancels_pending_reminder(self):
        target = post()
        self.store.update_schedule('test', 'codex', target, validate(result(), target, []))
        notifier = Notifier([Telegram('secret', '1')], self.store)
        with patch.object(notifier.channels[0], 'send', side_effect=ApiError('Telegram')):
            with self.assertRaises(ApiError):
                send_reminders(self.store, notifier, 'UTC', [10], BASE + timedelta(hours=3, minutes=-10))
        self.store.update_schedule('test', 'codex', target, unknown(target, status='cancelled'))
        self.assertEqual(self.store.db.execute('SELECT state FROM outbox').fetchone()[0], 'cancelled')
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM retries WHERE phase='reminder'").fetchone()[0], 0)
        with patch.object(notifier.channels[0], 'send') as send:
            send_reminders(self.store, notifier, 'UTC', [10], BASE + timedelta(hours=3))
            send.assert_not_called()

    def test_reminder_short_retry_and_stale_expiry(self):
        target, when = post(), BASE + timedelta(hours=3)
        self.store.update_schedule('test', 'codex', target, validate(result(), target, []))
        channel = Telegram('secret', '1')
        notifier = Notifier([channel], self.store)
        with patch.object(channel, 'send', side_effect=ApiError('Telegram')) as send:
            with self.assertRaises(ApiError):
                send_reminders(self.store, notifier, 'UTC', [0], when)
            send.side_effect = None
            self.assertEqual(send_reminders(self.store, notifier, 'UTC', [0], when + timedelta(seconds=121)), 1)
        self.store.update_schedule('test', 'codex', target, Assessment(True, when + timedelta(hours=1)))
        with patch.object(channel, 'send') as send:
            self.assertEqual(send_reminders(self.store, notifier, 'UTC', [0], when + timedelta(hours=2)), 0)
            send.assert_not_called()

    def test_health_repeat_and_recovery_survive_restart(self):
        notifier = Mock()
        error = WebFetchError('blocked')
        health = HealthNotice('test', self.store, repeat_seconds=3600)
        health.update(notifier, error, now=1000)
        resumed = HealthNotice('test', self.store, repeat_seconds=3600)
        resumed.update(notifier, error, now=1001)
        self.assertEqual(notifier.send.call_count, 1)
        resumed.update(notifier, error, now=4600)
        self.assertEqual(notifier.send.call_count, 2)
        resumed.update(notifier, now=4601)
        self.assertEqual(notifier.send.call_count, 3)

    def test_failed_health_delivery_does_not_change_event_key(self):
        health, notifier = HealthNotice('test', self.store), Mock()
        notifier.send.side_effect = ApiError('Telegram')
        health.update(notifier, AIError('first'), now=1000)
        first = notifier.send.call_args.kwargs['event_id']
        resumed = HealthNotice('test', self.store)
        notifier.send.side_effect = None
        resumed.update(notifier, AIError('different detail'), now=1001)
        self.assertEqual(first, notifier.send.call_args.kwargs['event_id'])

    def test_account_backoff_does_not_slow_healthy_account(self):
        schedule = PollSchedule(300)
        schedule.record('bad', 0, WebFetchError('blocked', retry_after=900))
        schedule.record('good', 0)
        self.assertTrue(schedule.due('good', 300))
        self.assertFalse(schedule.due('bad', 300))
        self.assertTrue(schedule.due('bad', 900))

    def test_monitor_collects_healthy_profile_during_other_backoff(self):
        clock = [0.0]
        bad, good = Mock(last_posts=[], last_visible_count=0), Mock(last_posts=[], last_visible_count=0)
        bad.fetch.side_effect = WebFetchError('blocked', retry_after=900)
        def sleep(delay):
            clock[0] += delay
            if clock[0] > 300:
                raise StopIteration
        with patch('bot.time.monotonic', side_effect=lambda: clock[0]), patch('bot.time.sleep', side_effect=sleep):
            with self.assertRaises(StopIteration):
                run_monitor([('bad', 'codex'), ('good', 'claude')], {'bad': bad, 'good': good}, self.store, None, 'UTC', 'UTC', 300, 24, None, 10)
        self.assertEqual(bad.fetch.call_count, 1)
        self.assertEqual(good.fetch.call_count, 2)

    def test_later_web_error_does_not_mask_ai_outage(self):
        target = dict(post(), created_at=datetime.now(UTC).isoformat())
        good = Mock(last_posts=[target], last_visible_count=1)
        bad = Mock(last_posts=[], last_visible_count=0)
        bad.fetch.side_effect = WebFetchError('blocked')
        analyzer = Mock(model='', analyze=Mock(side_effect=AIError('mock AI failure')))
        good.thread.return_value = target, []
        notifier = Mock()
        with patch('bot.time.sleep', side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                run_monitor([('good', 'codex'), ('bad', 'claude')], {'good': good, 'bad': bad}, self.store, notifier, 'UTC', 'UTC', 300, 24, analyzer, 10)
        notices = [c.args[0] for c in notifier.send.call_args_list]
        self.assertTrue(any('@good Codex AI' in n and 'mock AI failure' in n for n in notices))

    def test_status_is_read_only_without_credentials_or_database(self):
        path = self.disk_path()
        with patch.dict(os.environ, {'STATE_PATH': str(path)}, clear=True), patch('bot.load_dotenv'), patch.object(sys, 'argv', ['bot.py', '--status']), patch('builtins.print'):
            self.assertEqual(bot.main(), 0)
        self.assertFalse(path.exists())

    def test_status_and_history_do_not_write(self):
        path = self.disk_path()
        store = Store(str(path))
        store.cutoff('test', BASE)
        store.runtime('test', fetch_ok=BASE.timestamp(), web_error='blocked')
        store.enqueue('x', 'notice')
        store.receipt('x', 'destination-hash', 0)
        store.finish_event('x')
        store.db.close()
        before = path.read_bytes()
        output = io.StringIO()
        with patch('sys.stdout', output):
            inspect_state(path, 'UTC')
            inspect_state(path, 'UTC', 20)
        self.assertEqual(before, path.read_bytes())
        self.assertIn('blocked', output.getvalue())
        self.assertIn('notice', output.getvalue())

    def test_original_database_schema_migrates_without_resetting_seen(self):
        path = self.disk_path()
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE seen (account TEXT, post_id TEXT, PRIMARY KEY(account,post_id))')
        db.execute("INSERT INTO seen VALUES ('test','123')")
        db.execute('CREATE TABLE web_monitor (account TEXT PRIMARY KEY, cutoff TEXT NOT NULL)')
        db.execute('INSERT INTO web_monitor VALUES (?,?)', ('test', BASE.isoformat()))
        db.commit()
        db.close()
        store = Store(str(path))
        try:
            self.assertTrue(store.has_seen('test', '123'))
            self.assertEqual(store.cutoff('test', BASE + timedelta(days=1)), BASE)
            self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            store.db.close()

    def test_retention_keeps_pending_and_watched_posts_and_receipts(self):
        old = post(post_id='100')
        pending = post(post_id='101')
        watched = post(post_id='102')
        self.store.remember('test', [old, pending, watched])
        self.store.advance('test', old['id'])
        self.store.advance('test', watched['id'])
        self.store.watch('test', watched, BASE + timedelta(days=200))
        with patch('bot.time.time', return_value=BASE.timestamp()):
            self.store.enqueue('done', 'sent')
            self.store.receipt('done', 'destination', 0)
            self.store.finish_event('done')
            self.store.enqueue('unsent', 'pending')
            self.store.receipt('unsent', 'destination', 0)
        self.store.prune(90, now=(BASE + timedelta(days=100)).timestamp())
        self.assertEqual({r[0] for r in self.store.db.execute('SELECT post_id FROM posts')}, {'101', '102'})
        self.assertIsNone(self.store.event('done'))
        self.assertIsNotNone(self.store.event('unsent'))
        self.assertTrue(self.store.received('unsent', 'destination', 0))
        self.assertTrue(self.store.has_seen('test', '100'))

    def test_rate_limit_body_and_destination_cooldown(self):
        response = Mock(ok=False, status_code=429, headers={})
        response.json.return_value = {'parameters': {'retry_after': 900}}
        with patch('bot.HTTP.request', return_value=response):
            with self.assertRaises(ApiError) as caught:
                bot.request_json('Telegram', 'POST', 'https://example.invalid')
        self.assertEqual(caught.exception.retry_after, 900)
        channel = Telegram('secret', '1')
        notifier = Notifier([channel], self.store)
        with patch('bot.time.time', return_value=1000), patch.object(channel, 'send', side_effect=ApiError('Telegram', 429, 900)) as send:
            for event in ('first', 'second'):
                with self.assertRaises(ApiError):
                    notifier.send('notice', event_id=event)
            self.assertEqual(send.call_count, 1)
        with patch('bot.time.time', return_value=1901), patch.object(channel, 'send') as send:
            notifier.send('notice', event_id='second')
            send.assert_called_once()

    def test_long_telegram_text_splits_without_utf16_overflow(self):
        channel = Telegram('secret', '1')
        with patch.object(channel, 'send') as send:
            Notifier([channel], self.store).send('🎉' * 2800, event_id='long')
        self.assertGreater(send.call_count, 1)
        self.assertTrue(all(len(c.args[0].encode('utf-16-le')) // 2 <= 4096 for c in send.call_args_list))

    def test_cancellation_disables_reminder_even_when_notice_fails(self):
        target = post()
        self.store.update_schedule('test', 'codex', target, validate(result(), target, []), observed_ts=100)
        self.store.watch('test', target, BASE + timedelta(hours=6))
        reply = dict(post('Reset cancelled.', '124'), in_reply_to_id=target['id'])
        analyzer = Mock(model='', analyze=Mock(side_effect=lambda t, *_: unknown(t, status='cancelled')))
        notifier = Mock(send=Mock(side_effect=ApiError('Telegram')))
        with self.assertRaises(ApiError):
            follow_up('test', self.store, notifier, 'UTC', analyzer, 'codex', Mock(return_value=(target, [reply])), BASE)
        self.assertEqual(self.store.db.execute('SELECT active FROM schedules').fetchone()[0], 0)

    def test_old_retried_notice_does_not_revive_newer_cancelled_schedule(self):
        target = post()
        timed = validate(result(), target, [])
        self.store.update_schedule('test', 'codex', target, timed, observed_ts=100)
        self.store.update_schedule('test', 'codex', target, unknown(target, status='cancelled'), observed_ts=200)
        self.store.update_schedule('test', 'codex', target, timed, observed_ts=100)
        self.assertEqual(self.store.db.execute('SELECT active FROM schedules').fetchone()[0], 0)

    def test_partial_failure_notice_still_gets_a_recovery(self):
        notifier = Mock(send=Mock(side_effect=ApiError('Telegram')))
        health = HealthNotice('test', self.store)
        health.update(notifier, AIError('error'), now=1000)
        self.assertFalse(health.notified)
        notifier.send.side_effect = None
        health.update(notifier, now=1001)
        self.assertIn('복구', notifier.send.call_args.args[0])
        self.assertIsNone(health.state['since'])

    def test_once_reports_deferred_failed_work(self):
        target = dict(post(), created_at=datetime.now(UTC).isoformat())
        self.store.remember('test', [target])
        self.store.failed('test', target['id'], 'process', AIError('timeout'))
        client = Mock(last_posts=[], last_visible_count=0)
        code = run_monitor([('test', 'codex')], {'test': client}, self.store, None, 'UTC', 'UTC', 300, 24, None, 10, once=True)
        self.assertEqual(code, 1)

    def test_bare_id_mention_is_not_a_reference_to_another_post_time(self):
        target = post('Reset code 122', '123')
        older = post('in three hours', '122')
        data = result(target, time_source_post_id='122', evidence=[{'post_id': '123', 'quote': target['text']}, {'post_id': '122', 'quote': older['text']}])
        with self.assertRaises(AIError):
            validate(data, target, [older])


if __name__ == '__main__':
    unittest.main()
