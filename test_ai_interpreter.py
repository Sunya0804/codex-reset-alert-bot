import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from ai_interpreter import AIError, Analysis, CodexAnalyzer, child_environment, validate
from bot import ApiError, Store, ai_message, process


def post(text='Codex limits will reset in three hours.', post_id='123'):
    return {'id': post_id, 'text': text, 'created_at': '2026-10-19T01:00:00+00:00'}


def result(target=None, **changes):
    target = target or post()
    data = {'relevant': True, 'status': 'scheduled', 'interpretation': '게시 3시간 후 코덱스 사용 한도 리셋 예정이라는 공지입니다.', 'confidence': 'high', 'time_kind': 'relative', 'time_expression': 'in three hours', 'time_source_post_id': target['id'], 'relative_minutes': 180, 'scheduled_at': None, 'uncertainty': '실제 완료 여부는 확인하지 않았습니다.', 'evidence': [{'post_id': target['id'], 'quote': target['text']}]}
    data.update(changes)
    return data


class AITests(unittest.TestCase):
    def test_relative_time_and_notification_format(self):
        analysis = validate(result(), post(), [])
        self.assertEqual(analysis.when.hour, 4)
        notice = ai_message('thsottiaux', post(), analysis, 'Asia/Seoul')
        for value in ['원본 메시지 :', 'AI 해석 :', '예정 시간 : 2026-10-19 13:00 KST', '판단 확신도', '근거', '/status/123']:
            self.assertIn(value, notice)

    def test_evidence_invention_is_rejected(self):
        for evidence in [[{'post_id': '999', 'quote': 'reset'}], [{'post_id': '123', 'quote': 'at 10am PT'}], []]:
            with self.subTest(evidence=evidence), self.assertRaises(AIError):
                validate(result(evidence=evidence), post(), [])

    def test_naive_or_ungrounded_explicit_time_is_rejected(self):
        for phrase, timestamp in [('tomorrow morning', '2026-10-20T10:00:00+09:00'), ('tomorrow at 10am PT', '2026-10-20T10:00:00')]:
            target = post('Codex reset ' + phrase)
            with self.assertRaises(AIError):
                validate(result(target, time_kind='explicit', relative_minutes=None, scheduled_at=timestamp, time_expression=phrase), target, [])

    def test_time_from_unrelated_nearby_post_is_rejected(self):
        target = post('Done', '124')
        data = result(target, time_source_post_id='123', evidence=[{'post_id': '124', 'quote': 'Done'}, {'post_id': '123', 'quote': post()['text']}])
        with self.assertRaises(AIError):
            validate(data, target, [post()])

    def test_unknown_time_is_kept_unknown(self):
        target = post('Day 13/ Le limit reset')
        analysis = validate(result(target, time_kind='unknown', time_expression='', time_source_post_id=None, relative_minutes=None), target, [])
        self.assertIsNone(analysis.when)

    def test_completed_and_uncertain_cannot_have_future_time(self):
        for changes in [{'status': 'completed'}, {'confidence': 'low'}]:
            with self.assertRaises(AIError):
                validate(result(**changes), post(), [])

    def test_message_stays_within_telegram_limit_even_for_emoji(self):
        target = post('🎉' * 9000)
        data = result(target, interpretation='🎉' * 700, uncertainty='🎉' * 250)
        notice = ai_message('thsottiaux', target, Analysis(data, None), 'Asia/Seoul')
        self.assertLessEqual(len(notice.encode('utf-16-le')) // 2, 4096)
        self.assertTrue(notice.endswith('/status/123'))

    def test_child_does_not_inherit_keys_or_dochi_routes(self):
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'secret', 'OPENAI_API_KEY': 'secret', 'CODEX_API_KEY': 'secret', 'DOCHI_CODEX_PROGRESS_TOKEN': 'secret'}):
            env = child_environment()
        for key in ['TELEGRAM_BOT_TOKEN', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'DOCHI_CODEX_PROGRESS_TOKEN']:
            self.assertNotIn(key, env)

    @patch('ai_interpreter.cli_command', return_value=['codex.exe'])
    @patch('ai_interpreter.subprocess.run')
    def test_cli_uses_stdin_saved_auth_schema_and_no_shell(self, run, command):
        run.return_value = Mock(returncode=0, stdout=json.dumps(result()), stderr='')
        analyzer = CodexAnalyzer()
        analyzer.analyze(post('Codex limits will reset in three hours.'), [])
        args = run.call_args.args[0]
        self.assertIn('--output-schema', args)
        self.assertIn('--ignore-user-config', args)
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertIn('features.shell_tool=false', args)
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertIn('three hours', run.call_args.kwargs['input'])
        self.assertNotIn('three hours', ' '.join(args))
        self.assertIn('OpenAI Codex', run.call_args.kwargs['input'])
        analyzer.analyze(post('Codex limits will reset in three hours.'), [], 'claude')
        self.assertIn('Anthropic Claude', run.call_args.kwargs['input'])

    @patch('ai_interpreter.cli_command', return_value=['codex.exe'])
    @patch('ai_interpreter.subprocess.run')
    def test_failed_cli_and_bad_json_do_not_leak_stderr(self, run, command):
        run.return_value = Mock(returncode=1, stdout='', stderr='SECRET_TOKEN')
        with self.assertRaises(AIError) as caught:
            CodexAnalyzer().analyze(post(), [])
        self.assertNotIn('SECRET_TOKEN', str(caught.exception))
        run.return_value = Mock(returncode=0, stdout='not json')
        with self.assertRaises(AIError):
            CodexAnalyzer().analyze(post(), [])
        run.side_effect = subprocess.TimeoutExpired('codex', 1)
        with self.assertRaises(AIError):
            CodexAnalyzer().analyze(post(), [])

    def test_ai_failure_preserves_pending_even_if_page_later_loses_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 'state.sqlite3'))
            target, analyzer, telegram = post(), Mock(model=''), Mock()
            store.remember('test', [target])
            analyzer.analyze.side_effect = AIError('timeout')
            with self.assertRaises(AIError):
                process([target], 'test', store, telegram, 'UTC', 'UTC', analyzer=analyzer)
            self.assertFalse(store.has_seen('test', target['id']))
            self.assertEqual(len(store.pending('test', datetime(2026, 10, 18, tzinfo=timezone.utc))), 1)
            telegram.send.assert_not_called()
            store.db.close()

    def test_successful_ai_cache_survives_telegram_failure_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'state.sqlite3')
            store, target, analyzer, telegram = Store(path), post(), Mock(model=''), Mock()
            analyzer.analyze.return_value = validate(result(), target, [])
            telegram.send.side_effect = ApiError('Telegram')
            with self.assertRaises(ApiError):
                process([target], 'test', store, telegram, 'UTC', 'UTC', analyzer=analyzer)
            store.db.close()
            store = Store(path)
            telegram.send.side_effect = None
            process([target], 'test', store, telegram, 'UTC', 'UTC', analyzer=analyzer)
            self.assertEqual(analyzer.analyze.call_count, 1)
            self.assertTrue(store.has_seen('test', '123'))
            store.db.close()

    def test_no_keyword_prefilter_and_unrelated_posts_are_not_sent(self):
        target = post('Your quota will be restored in three hours.')
        with tempfile.TemporaryDirectory() as tmp:
            store, analyzer, telegram = Store(str(Path(tmp) / 's.db')), Mock(model=''), Mock()
            analyzer.analyze.return_value = validate(result(target), target, [])
            process([target], 'test', store, telegram, 'UTC', 'UTC', analyzer=analyzer)
            telegram.send.assert_called_once()
            other = post('Lunch time', '124')
            analyzer.analyze.return_value = validate(result(other, relevant=False, status='unrelated', time_kind='unknown', time_expression='', time_source_post_id=None, relative_minutes=None, evidence=[]), other, [])
            process([other], 'test', store, telegram, 'UTC', 'UTC', analyzer=analyzer)
            self.assertEqual(telegram.send.call_count, 1)
            self.assertTrue(store.has_seen('test', '124'))
            store.db.close()

    def test_author_reply_supplies_time_and_follow_up_sends_update(self):
        from bot import follow_up
        target = post('Day 17/ it’s reset day today.', '200')
        reply = dict(post('Lands tomorrow before 10am PST.', '201'), in_reply_to_id='200')
        unknown = result(target, time_kind='unknown', time_expression='', time_source_post_id=None, relative_minutes=None)
        # 01:00 UTC on Oct 19 is Oct 18 in PST, so "tomorrow" is Oct 19 PST.
        timed = result(target, time_kind='explicit', relative_minutes=None, scheduled_at='2026-10-19T10:00:00-08:00', time_expression='tomorrow before 10am PST', time_source_post_id='201', evidence=[{'post_id': '200', 'quote': target['text']}, {'post_id': '201', 'quote': reply['text']}])
        # A reply's time is accepted only when the reply is actually attached to the target.
        with self.assertRaises(AIError):
            validate(timed, target, [reply])
        self.assertEqual(validate(timed, dict(target, replies=[reply]), []).when.astimezone(timezone.utc).hour, 18)
        with tempfile.TemporaryDirectory() as tmp:
            store, analyzer, notifier = Store(str(Path(tmp) / 's.db')), Mock(model=''), Mock()
            analyzer.analyze.side_effect = lambda t, c, p: validate(timed if t.get('replies') else unknown, t, c)
            thread = Mock(return_value=(dict(target, text=target['text'] + ' (full)'), []))
            process([target], 'test', store, notifier, 'UTC', 'UTC', analyzer=analyzer, thread=thread)
            self.assertIn('(full)', notifier.send.call_args.args[0])
            now = datetime(2026, 10, 19, 2, tzinfo=timezone.utc)
            self.assertEqual(follow_up('test', store, notifier, 'UTC', analyzer, 'codex', thread, now), 0)
            thread.return_value = (target, [reply])
            self.assertEqual(follow_up('test', store, notifier, 'UTC', analyzer, 'codex', thread, now), 1)
            update = notifier.send.call_args.args[0]
            self.assertIn('후속 답글', update)
            self.assertIn('↳ Lands tomorrow', update)
            self.assertIn('2026-10-19 18:00', update)
            self.assertEqual(len(store.watched('test', now)), 1)  # Keep watching for changes/cancellation.
            store.db.close()

    def test_follow_up_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / 's.db'))
            store.watch('test', post(), datetime(2026, 10, 19, 7, tzinfo=timezone.utc))
            self.assertEqual(len(store.watched('test', datetime(2026, 10, 19, 6, tzinfo=timezone.utc))), 1)
            self.assertEqual(store.watched('test', datetime(2026, 10, 19, 8, tzinfo=timezone.utc)), [])
            store.db.close()


if __name__ == '__main__':
    unittest.main()
