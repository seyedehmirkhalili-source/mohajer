import base64
import html
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_shop import ALICE, BOB, ShopCase, bot, cb, shop


def unpack(messages):
    return [html.unescape(message[5:-6]) for message in messages]


class ConfigMessageTests(unittest.TestCase):
    def test_reality_configs_come_first_preserving_each_groups_order(self):
        other = ['vless://tls?security=tls#Reality', 'trojan://plain#?security=reality',
                 'vless://none?security=none&label=security%3Dreality']
        reality = ['vless://first?encryption=none&security=reality&type=tcp#first',
                   'vless://second?security=%72eality#second']
        text = '\n'.join([other[0], reality[0], other[1], reality[1], other[2]])
        self.assertEqual(unpack(shop.config_messages(text)), ['\n'.join(reality + other)])

    def test_reality_order_is_applied_before_message_splitting(self):
        other = ['vless://' + str(i) + 'x' * 990 + '?security=tls' for i in range(5)]
        reality = ['vless://reality' + str(i) + 'y' * 990 + '?security=reality' for i in range(5)]
        chunks = unpack(shop.config_messages('\n'.join(other + reality)))
        self.assertGreater(len(chunks), 1)
        self.assertEqual('\n'.join(chunks).splitlines(), reality + other)
        self.assertTrue(all(len(chunk.encode('utf-16-le')) // 2 <= 4096 for chunk in chunks))

    def test_packs_whole_configs_greedily_in_order(self):
        lines = ['vless://' + str(i) + 'x' * 990 for i in range(11)]
        messages = shop.config_messages('\n'.join(lines))
        chunks = unpack(messages)
        self.assertEqual([len(chunk.splitlines()) for chunk in chunks], [4, 4, 3])
        self.assertEqual('\n'.join(chunks).splitlines(), lines)
        self.assertTrue(all(message.count('<pre>') == 1 and message.count('</pre>') == 1
                            for message in messages))

    def test_exact_limit_and_newline_at_boundary(self):
        first = 'vless://' + 'x' * (4096 - len('vless://'))
        self.assertEqual(unpack(shop.config_messages(first)), [first])
        self.assertEqual(unpack(shop.config_messages(first + '\nvless://next')),
                         [first, 'vless://next'])
        lines = ['vless://' + 'x' * 2039, 'trojan://' + 'y' * 2039]
        self.assertEqual(len('\n'.join(lines)), 4096)
        self.assertEqual(unpack(shop.config_messages('\n'.join(lines))), ['\n'.join(lines)])

    def test_escapes_html_without_counting_encoded_entities(self):
        line = 'vless://id@host?x=<pre>&y="a"#' + '&' * 2040
        messages = shop.config_messages(line + '\n' + line)
        self.assertEqual(unpack(messages), [line, line])
        self.assertIn('&lt;pre&gt;&amp;', messages[0])
        self.assertEqual(unpack(shop.config_messages('&' * 4096)), ['&' * 4096])

    def test_emoji_counts_as_two_utf16_units(self):
        line = 'vless://' + '😀' * 2044
        self.assertEqual(len(line.encode('utf-16-le')) // 2, 4096)
        self.assertEqual(unpack(shop.config_messages(line + '\nvless://next')),
                         [line, 'vless://next'])
        with self.assertRaises(ValueError):
            shop.config_messages(line + 'x')

    def test_oversized_config_is_rejected_before_any_partial_result(self):
        with self.assertRaises(ValueError):
            shop.config_messages('vless://good\n' + 'vless://' + 'x' * 4090)

    def test_empty_lines_and_crlf(self):
        self.assertEqual(shop.config_messages(' \r\n\n'), [])
        self.assertEqual(unpack(shop.config_messages('vless://a\r\n\r\ntrojan://b\r\n')),
                         ['vless://a\ntrojan://b'])


class ConfigDeliveryTests(ShopCase):
    def setUp(self):
        super().setUp()
        self.token = self.fake_create(30, 30, 'Alice')
        c = bot.db()
        c.execute("INSERT INTO shop_services(token,tg_id,kind,created_ts) VALUES(?,?,'paid',1)",
                  (self.token, ALICE))
        c.commit(); c.close()

    def export(self, text):
        with tempfile.TemporaryDirectory() as folder, patch.object(bot, 'SUB_DIR', folder):
            def fresh(*args):
                Path(bot.sub_path(self.token)).write_bytes(base64.b64encode(text.encode()))
            with patch.object(bot, 'write_sub', side_effect=fresh), patch.object(shop, 'start_connection_export', side_effect=shop.send_connection):
                bot.handle_update(cb(ALICE, 'c:configs:' + self.token))

    def test_existing_callback_delivers_grouped_text_and_never_a_document(self):
        lines = ['vless://' + str(i) + 'x' * 990 for i in range(9)]
        with patch.object(shop, 'upload') as upload, patch.object(shop.time, 'sleep') as sleep:
            self.export('\n'.join(lines))
            upload.assert_not_called()
        sent = [p for m, p in self.calls if m == 'sendMessage']
        self.assertEqual([p['chat_id'] for p in sent], [ALICE] * 3)
        self.assertTrue(all(p['parse_mode'] == 'HTML' for p in sent))
        self.assertEqual('\n'.join(unpack([p['text'] for p in sent])).splitlines(), lines)
        self.assertEqual(sleep.call_args_list, [((1,),), ((1,),)])

    def test_export_sends_reality_first_without_rewriting_config_content(self):
        tls = 'vless://tls@host?security=tls&type=ws#Reality-label'
        reality = 'vless://direct@host?security=reality&type=tcp#direct'
        self.export(tls + '\n' + reality)
        self.assertEqual(unpack(self.texts(ALICE)), [reality + '\n' + tls])

    def test_failed_send_stops_before_later_chunks_and_is_not_retried(self):
        original = bot.tg
        attempts = []
        def fail_second(method, **params):
            if method == 'sendMessage' and params['text'].startswith('<pre>'):
                attempts.append(params['text'])
                if len(attempts) == 2: return {}
            return original(method, **params)
        with patch.object(bot, 'tg', side_effect=fail_second), patch.object(shop.time, 'sleep'):
            self.export('\n'.join('vless://' + str(i) + 'x' * 990 for i in range(13)))
        self.assertEqual(len(attempts), 2)
        self.assertIn('کامل نشد', self.texts(ALICE)[-1])

    def test_single_oversized_config_has_no_partial_messages_or_file_fallback(self):
        with patch.object(shop, 'upload') as upload:
            self.export('vless://good\n' + 'vless://' + 'x' * 4090)
            upload.assert_not_called()
        self.assertFalse(any(text.startswith('<pre>') for text in self.texts(ALICE)))
        self.assertIn('لینک اشتراک', self.texts(ALICE)[-1])

    def test_empty_export_explains_no_configs(self):
        self.export('\n')
        self.assertIn('فعلاً کانفیگی', self.texts(ALICE)[-1])

    def test_other_customer_cannot_export(self):
        with patch.object(bot, 'write_sub') as write, patch.object(shop, 'upload') as upload:
            shop.send_connection(BOB, BOB, self.token, 'configs')
        write.assert_not_called(); upload.assert_not_called()
        self.assertFalse(any(text.startswith('<pre>') for text in self.texts(BOB)))

    def test_background_export_allows_other_updates_and_prevents_overlap(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        real_thread = threading.Thread
        workers = []
        def thread(**kwargs):
            worker = real_thread(**kwargs); workers.append(worker); return worker
        def blocked(*args):
            entered.set()
            try: release.wait(3)
            finally: finished.set()
        with patch.object(shop, 'send_connection', side_effect=blocked) as send, patch.object(shop.threading, 'Thread', side_effect=thread):
            try:
                bot.handle_update(cb(ALICE, 'c:configs:' + self.token))
                self.assertTrue(entered.wait(1))
                bot.handle_update(cb(ALICE, 'c:configs:' + self.token))
                self.assertEqual(send.call_count, 1)
                bot.handle_update(cb(BOB, 'c:svc:missing'))
                self.assertTrue(self.texts(BOB))
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                for worker in workers: worker.join(2)
        self.assertNotIn(ALICE, shop._connection_chats)

    def test_worker_capacity_and_start_failure_release_slot(self):
        with patch.object(shop, '_connection_chats', {1, 2, 3, 4}), patch.object(shop.threading, 'Thread') as thread:
            shop.start_connection_export(ALICE, ALICE, self.token, 'configs')
            thread.assert_not_called()
        with patch.object(shop.threading, 'Thread', side_effect=RuntimeError('cannot start')):
            with self.assertRaises(RuntimeError):
                shop.start_connection_export(ALICE, ALICE, self.token, 'configs')
        self.assertNotIn(ALICE, shop._connection_chats)
