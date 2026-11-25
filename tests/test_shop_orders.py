import time
import unittest

from tests.test_shop import ALICE, BOB, ADMIN, SUPPORT, SalesCase, bot, shop, cb, msg, photo


class OrderTrackingTests(SalesCase):
    def test_empty_history_rules_and_editable_terms(self):
        bot.handle_update(msg(ALICE, shop.B_ORDERS))
        self.assertIn("هنوز سفارشی", self.last()["text"])
        shop.set_setting("terms", "قوانین تازه <متن>")
        bot.handle_update(msg(ALICE, shop.B_RULES))
        self.assertIn("&lt;متن&gt;", self.last()["text"])
        self.assertIsNone(self.row("SELECT terms_acceptance FROM shop_customers WHERE tg_id=?", (ALICE,))[0])

    def test_manual_purchase_from_plan_through_receipt_to_service(self):
        order = self.buy()
        self.assertEqual(order["amount"], self.plan["price"])
        bot.handle_update(cb(ALICE, "c:order:%d" % order["id"]))
        self.assertIn("در انتظار پرداخت", self.last("editMessageText")["text"])
        self.assertIn("۱۵۰٬۰۰۰ تومان", self.last("editMessageText")["text"])
        bot.handle_update(photo(ALICE))
        self.assertIn("c:order:%d" % order["id"], str(self.last()["reply_markup"]))
        bot.handle_update(cb(ALICE, "c:order:%d" % order["id"]))
        self.assertIn("رسید در انتظار بررسی", self.last("editMessageText")["text"])
        self.assertIn("نیازی به پرداخت دوباره نیست", self.last("editMessageText")["text"])
        bot.handle_update(cb(SUPPORT, "r:ok:%d" % order["id"]))
        bot.handle_update(cb(ALICE, "c:order:%d" % order["id"]))
        self.assertIn("تحویل شده", self.last("editMessageText")["text"])
        buttons = [b.get("callback_data") for row in self.last("editMessageText")["reply_markup"]["inline_keyboard"] for b in row]
        token = shop.get_order(order["id"])["token"]
        self.assertIn("c:svc:%s" % token, buttons)
        before = len(self.minted)
        bot.handle_update(cb(SUPPORT, "r:ok:%d" % order["id"]))
        self.assertEqual(len(self.minted), before)

    def test_other_customer_cannot_read_order_card_amount_or_reason(self):
        order = self.buy()
        bot.handle_update(msg(BOB, "/start"))
        for oid in (order["id"], 99999):
            bot.handle_update(cb(BOB, "c:order:%d" % oid))
            text = self.last("editMessageText")["text"]
            self.assertEqual(text, "این سفارش پیدا نشد.")
            self.assertNotIn("603799", text)
        self.assertIn("هنوز سفارشی", shop.orders_view(BOB)[0])

    def test_approved_but_not_delivered_never_claims_delivery(self):
        order = self.buy()
        c = bot.db(); c.execute("UPDATE shop_orders SET status='approved' WHERE id=?", (order["id"],)); c.commit(); c.close()
        text, kb = shop.order_view(ALICE, order["id"])
        self.assertIn("در انتظار تحویل", text)
        self.assertIn("دوباره پرداخت نکن", text)
        self.assertNotIn("c:svc:", str(kb))

    def test_expired_invoice_shows_expiry_before_background_tick(self):
        order = self.buy()
        text, kb = shop.order_view(ALICE, order["id"], now=order["created_ts"] + shop.ORDER_TTL)
        self.assertIn("مهلت پرداخت گذشته", text)
        self.assertNotIn("603799", text)
        self.assertNotIn("c:cancel:", str(kb))

    def test_rejection_reason_is_escaped_and_order_support_opens(self):
        order = self.buy(); bot.handle_update(photo(ALICE))
        shop.reject(order["id"], ADMIN, "رسید <ناخوانا>")
        text, kb = shop.order_view(ALICE, order["id"])
        self.assertIn("رد شده", text); self.assertIn("&lt;ناخوانا&gt;", text)
        bot.handle_update(cb(ALICE, "c:report"))
        self.assertIn("گزارش اختلال", self.last()["text"])

    def test_page_size_and_stale_invalid_callbacks(self):
        for i in range(shop.ORDER_PAGE_SIZE + 1):
            shop.create_order(ALICE, "topup", 50000 + i)
        text, kb = shop.orders_view(ALICE)
        self.assertEqual(sum(b.get("callback_data", "").startswith("c:order:") for row in kb for b in row), shop.ORDER_PAGE_SIZE)
        self.assertIn("c:orders:1", str(kb))
        self.assertIn("c:orders:0", str(shop.orders_view(ALICE, 1)[1]))
        for data in ("c:orders:999999999999999", "c:orders:-1", "c:order:²", "c:order:broken"):
            bot.handle_update(cb(ALICE, data))

    def test_delivered_topup_is_not_a_service_and_auto_receipt_is_not_final(self):
        order = shop.create_order(ALICE, "topup", 50000)
        c = bot.db(); c.execute("UPDATE shop_orders SET status='approved',delivered_ts=1 WHERE id=?", (order["id"],)); c.commit(); c.close()
        text, kb = shop.order_view(ALICE, order["id"])
        self.assertIn("شارژ کیف پول", text); self.assertNotIn("c:svc:", str(kb))
        c = bot.db(); c.execute("UPDATE shop_orders SET auto_approved=1,auto_reviewed=0 WHERE id=?", (order["id"],)); c.commit(); c.close()
        self.assertIn("رسید در انتظار بررسی مدیر", shop.order_view(ALICE, order["id"])[0])


if __name__ == "__main__":
    unittest.main()
