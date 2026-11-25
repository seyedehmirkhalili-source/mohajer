#!/usr/bin/env python3
"""Offline acceptance walkthrough of the real update router and SQLite/subscription code.

No Telegram token, bank transfer or Xray server is used. Telegram and Xray side
effects are replaced at their boundaries; calls, database assertions and rendered
web pages are saved for inspection. Run from any directory:
python scripts/check_shop_flow.py --output dev/walkthrough
"""
import argparse
import base64
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sub"))
os.environ["DPBOT_ENV"] = str(ROOT / "dev" / "walkthrough-nonexistent.env")
from tests.test_shop import ALICE, BOB, ADMIN, SUPPORT, SalesCase, bot, shop, msg, cb, photo
import subserver


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    trace, checks = [], []
    case = SalesCase(methodName="runTest")
    case.setUp()

    def act(title, update):
        # The accelerated walkthrough must not trip a real ten-second rate limit.
        shop._hits.clear()
        before = len(case.calls)
        bot.handle_update(update)
        trace.append({"step": title, "input": update, "messages": copy.deepcopy(case.calls[before:])})

    def check(label, value):
        checks.append({"check": label, "passed": bool(value)})
        if not value: raise AssertionError(label)

    def buy(uid, pid, token=None):
        suffix = ":" + token if token else ""
        act("انتخاب پلن و دیدن قوانین", cb(uid, "c:plan:%d%s" % (pid, suffix)))
        act("پذیرفتن قوانین نمایش‌داده‌شده", cb(uid, "c:agree:%d%s" % (pid, suffix)))
        act("ساخت فاکتور کارت‌به‌کارت", cb(uid, "c:pay:%d%s" % (pid, suffix)))
        return case.row("SELECT * FROM shop_orders WHERE tg_id=? ORDER BY id DESC", (uid,))

    try:
        with tempfile.TemporaryDirectory() as subs, \
             patch.object(bot, "SUB_DIR", subs), \
             patch.object(bot, "SUB_BASE", "https://example.invalid"), \
             patch.object(bot, "DOMAIN", "example.invalid"), \
             patch.object(bot, "ENDPOINTS", [{"tag": "vless-ws", "proto": "vless", "net": "ws", "port": 10000,
                                              "path": "/walkthrough", "label": "VLESS", "tls_ports": [443], "notls_ports": []}]), \
             patch.object(bot, "_adu", return_value=True), \
             patch.object(bot, "_rmu_email", return_value=True), \
             patch.object(bot, "xr_online_map", return_value={}), \
             patch.object(bot, "force_disconnect", return_value=None), \
             patch.object(bot, "create_user", case._fns["create_user"]), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden in offline walkthrough")):
            act("ورود مدیر", msg(ADMIN, "/start"))
            act("پنل فروشگاه مدیر", msg(ADMIN, shop.B_SHOP))
            act("ورود مشتری", msg(ALICE, "/start"))
            act("نمایش قوانین بدون شروع خرید", msg(ALICE, shop.B_RULES))
            act("دریافت تست", cb(ALICE, "c:test"))
            check("trial created in real SQLite", case.row("SELECT COUNT(*) FROM shop_services WHERE kind='test'")[0] == 1)
            act("دیدن فهرست پلن‌ها", msg(ALICE, shop.B_BUY))
            o = buy(ALICE, case.plan["id"])
            check("invoice is exactly displayed price", o["amount"] == 150000)
            act("پیگیری فاکتور پیش از پرداخت", cb(ALICE, "c:order:%d" % o["id"]))
            act("ارسال رسید آزمایشی", photo(ALICE, "walkthrough-receipt"))
            check("receipt waits for reviewer", shop.get_order(o["id"])["status"] == "review")
            act("پیگیری رسید در صف", cb(ALICE, "c:order:%d" % o["id"]))
            act("تأیید رسید با نقش پشتیبان", cb(SUPPORT, "r:ok:%d" % o["id"]))
            delivered = shop.get_order(o["id"])
            check("approved order actually delivered", delivered["status"] == "approved" and delivered["delivered_ts"])
            token = delivered["token"]
            check("subscription file written by real core", Path(bot.sub_path(token)).exists())
            check("subscription contains a VLESS config", b"vless://" in base64.b64decode(Path(bot.sub_path(token)).read_bytes()))
            act("پیگیری تحویل", cb(ALICE, "c:order:%d" % o["id"]))
            act("دیدن مصرف و سرویس", cb(ALICE, "c:svc:%s" % token))
            # Use the real exporter synchronously, then record its messages.
            before = len(case.calls)
            shop.send_connection(ALICE, ALICE, token, "configs")
            trace.append({"step": "دریافت کانفیگ محلی بدون سایت", "input": {"callback": "c:configs:<demo-token>"},
                          "messages": copy.deepcopy(case.calls[before:])})
            check("config text reaches Telegram transport", any("vless://" in p.get("text", "") for _, p in case.calls[before:]))
            before_count = case.row("SELECT COUNT(*) FROM users")[0]
            act("تأیید تکراری رسید", cb(SUPPORT, "r:ok:%d" % o["id"]))
            check("duplicate approval creates no extra service", case.row("SELECT COUNT(*) FROM users")[0] == before_count)
            act("ورود مشتری دوم", msg(BOB, "/start"))
            act("آزمون دسترسی به سفارش دیگری", cb(BOB, "c:order:%d" % o["id"]))
            check("foreign order hidden", case.last("editMessageText")["text"] == "این سفارش پیدا نشد.")
            # A cancelled order must remain visible without changing ledger or payment state.
            b = buy(BOB, case.plan["id"])
            act("لغو سفارش پرداخت‌نشده", cb(BOB, "c:cancel:%d" % b["id"]))
            act("دیدن سفارش لغوشده", cb(BOB, "c:order:%d" % b["id"]))
            check("cancelled order retained", shop.get_order(b["id"])["status"] == "cancelled")
            act("شروع پشتیبانی", cb(ALICE, "c:report"))
            act("ادامه ثبت مشکل", cb(ALICE, "c:rep_go"))
            act("انتخاب سرویس برای تیکت", cb(ALICE, "c:rs:%s" % token))
            act("انتخاب سؤال عمومی", cb(ALICE, "c:rc:other"))
            act("متن تیکت با شماره سفارش", msg(ALICE, "سفارش #%d؛ برای وارد کردن کانفیگ راهنمایی می‌خواهم." % o["id"]))
            ticket = case.row("SELECT * FROM shop_tickets ORDER BY id DESC")
            check("ticket contains service ownership", ticket["tg_id"] == ALICE and ticket["token"] == token)
            act("پاسخ پشتیبان", cb(SUPPORT, "t:r:%d" % ticket["id"]))
            act("متن پاسخ پشتیبان", msg(SUPPORT, "کانفیگ را کپی کن و از گزینهٔ ورود از کلیپ‌بورد در برنامه استفاده کن."))
            act("بستن تیکت", cb(SUPPORT, "t:x:%d" % ticket["id"]))
            check("support conversation closed", case.row("SELECT status FROM shop_tickets WHERE id=?", (ticket["id"],))[0] == "closed")
            act("فهرست سفارش‌ها", msg(ALICE, shop.B_ORDERS))
            check("direct purchase does not debit wallet", shop.balance(ALICE) == 0)
            output.joinpath("admin-preview.html").write_text(bot.render_dashboard("offline-preview"), encoding="utf-8")
            u = dict(case.row("SELECT * FROM users WHERE token=?", (token,)))
            response = subserver.build_response("sub-u-" + token, Path(bot.sub_path(token)).read_text(), u, "Mozilla/5.0", False)
            check("subscriber HTML generated by real server function", response[0] == 200 and "text/html" in response[1])
            output.joinpath("subscriber-preview.html").write_bytes(response[2])
        result = {"mode": "offline-real-router-sqlite-and-sub-files; mocked-telegram-and-xray", "checks": checks,
                  "steps": trace, "real_payment": False, "telegram_network_test": False}
        output.joinpath("walkthrough.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"checks": len(checks), "passed": sum(x["passed"] for x in checks), "steps": len(trace), "output": str(output)}, ensure_ascii=False))
    finally:
        case.tearDown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dev" / "walkthrough")
    run(parser.parse_args().output.resolve())
