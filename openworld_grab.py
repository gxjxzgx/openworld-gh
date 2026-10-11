#!/usr/bin/env python3
"""
Openworld 免费 VPS 库存监控 + 自动领取 (for GitHub Actions, 每 10 分钟一轮)

流程:
  1. 恢复上轮保存的 cookie(如有), 访问 /createvps
  2. 若被踢到登录页: 填邮箱 -> Continue -> 填密码 -> 提交
     若 Clerk 要邮箱验证码: 用 Gmail IMAP(App Password)自动读取最新验证码填入
  3. 检查 "Free FREE PROXMOX" 是否有货
  4. 有货: Telegram 推送告警, 然后尝试自动领取(选 Debian 13, 走完 Finalize Deployment)
     若遇到验证码/CAPTCHA 挡住: 立即 Telegram 推送, 人工接手
  5. 保存 cookie, 结束

环境变量 (全部从 GitHub Secrets 注入, 不要写死在代码里):
  OPENWORLD_EMAIL       openworld 登录邮箱
  OPENWORLD_PASSWORD    openworld 登录密码
  GMAIL_APP_PASSWORD    Gmail 应用专用密码(用于 IMAP 自动读 Clerk 验证码, 可选)
  TG_BOT_TOKEN          Telegram Bot Token(可选, 用于推送)
  TG_CHAT_ID            Telegram Chat ID(可选)
  COOKIE_FILE           cookie 保存路径, 默认 cookies.json
  DRY_RUN=1             只查库存不领取(测试用)
"""

import imaplib
import email as email_lib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://openworld.eu.org"
EMAIL = os.environ.get("OPENWORLD_EMAIL", "")
PASSWORD = os.environ.get("OPENWORLD_PASSWORD", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
COOKIE_FILE = os.environ.get("COOKIE_FILE", "cookies.json")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def tg(msg):
    """Telegram 推送, 没配就不推, 不报错"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log(f"[TG未配置] {msg}")
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=15)
        log("TG 推送已发送")
    except Exception as e:
        log(f"TG 推送失败: {e}")


def get_clerk_code(timeout=150):
    """用 Gmail IMAP 等待并读取最新的 Clerk 验证码, 返回 6 位数字或 None"""
    if not GMAIL_APP_PASSWORD:
        log("未配置 GMAIL_APP_PASSWORD, 无法自动读验证码")
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            m = imaplib.IMAP4_SSL("imap.gmail.com")
            m.login(EMAIL, GMAIL_APP_PASSWORD)
            m.select("INBOX")
            # 搜最近 20 封, 找 Clerk 验证码邮件
            typ, data = m.search(None, "ALL")
            ids = data[0].split()[-20:]
            best = None
            for i in reversed(ids):
                typ, d = m.fetch(i, "(BODY.PEEK[] INTERNALDATE)")
                raw = d[0][1]
                msg = email_lib.message_from_bytes(raw)
                subj = str(msg.get("Subject", ""))
                frm = str(msg.get("From", ""))
                if "clerk" not in (subj + frm).lower() and "verification" not in subj.lower():
                    continue
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body += part.get_payload(decode=True).decode("utf-8", "ignore")
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", "ignore")
                codes = re.findall(r"\b(\d{6})\b", subj + "\n" + body)
                if codes:
                    # 取邮件日期, 只要 10 分钟内的
                    date_tuple = email_lib.utils.parsedate_tz(msg.get("Date", ""))
                    if date_tuple:
                        ts = email_lib.utils.mktime_tz(date_tuple)
                        if time.time() - ts < 600:
                            best = codes[0]
                            # 读到码后删除该邮件, 不留验证码在收件箱
                            try:
                                m.store(i, "+FLAGS", "\\Deleted")
                                m.expunge()
                                log("验证码邮件已删除")
                            except Exception as e:
                                log(f"删除验证码邮件失败: {e}")
                            break
            m.logout()
            if best:
                log("已从 Gmail 读到验证码")
                return best
        except Exception as e:
            log(f"IMAP 读码出错: {e}")
        time.sleep(10)
    log("等待验证码超时")
    return None


def do_login(page):
    """执行登录流程, 返回 True/False"""
    log("开始登录流程")
    page.goto(f"{BASE}/login", timeout=30000)
    page.wait_for_timeout(2500)

    # 0. 点 "Sign in" 按钮, 拉起 Clerk 登录框
    signin_btn = page.locator('button:has-text("Sign in")').first
    try:
        signin_btn.wait_for(timeout=10000)
        signin_btn.click()
        log('已点 "Sign in"')
        page.wait_for_timeout(3000)
    except Exception:
        log('"Sign in" 按钮没找到, 可能已在登录框内或页面结构变了')
        page.screenshot(path="login_no_signin.png")

    # 1. 填邮箱 (Clerk 用 name="identifier", 不是 email)
    email_input = page.locator(
        'input[name="identifier"], input[type="email"], input[name="email"], '
        'input[placeholder*="mail" i], input[placeholder*="邮箱" i]'
    ).first
    try:
        email_input.wait_for(timeout=20000)
    except Exception:
        page.screenshot(path="login_no_email.png")
        log(f"没找到邮箱输入框, url={page.url}, title={page.title()}")
        raise
    email_input.fill(EMAIL)
    log("邮箱已填")

    # 2. 点 Continue (用 role 定位, 避开页面上隐藏的按钮)
    cont = page.get_by_role("button", name="Continue")
    cont.click(timeout=15000)
    log("已点 Continue")
    page.wait_for_timeout(2500)

    # 3. 填密码
    pwd_input = page.locator('input[type="password"], input[name="password"]').first
    try:
        pwd_input.wait_for(timeout=15000)
    except Exception:
        page.screenshot(path="login_no_pwd.png")
        log(f"没找到密码框, url={page.url}")
        raise
    pwd_input.fill(PASSWORD)
    log("密码已填")

    # 4. 提交 (密码页的按钮也是 "Continue")
    submit = page.get_by_role("button", name="Continue")
    submit.click(timeout=15000)
    log("已提交登录")
    page.wait_for_timeout(4000)

    # 5. 是否要邮箱验证码 (等久一点, 看截图确认状态)
    page.wait_for_timeout(8000)
    page.screenshot(path="after_submit.png")
    body_text = page.inner_text("body")
    # 验证码框的多种特征: 文字提示 OR 数字输入框 OR Clerk 的 OTP 组件
    code_needed = (
        re.search(r"verification code|enter.*code|check your email|验证码|one-time", body_text, re.I)
        or page.locator('input[inputmode="numeric"], input[name="code"], input[autocomplete="one-time-code"]').count() > 0
    )
    if code_needed:
        log("Clerk 要求邮箱验证码, 尝试自动读取")
        code = get_clerk_code()
        if not code:
            tg("⚠️ Openworld 登录需要邮箱验证码, 自动读取失败, 请人工处理")
            return False
        code_input = page.locator(
            'input[inputmode="numeric"], input[name="code"], '
            'input[autocomplete="one-time-code"], input[placeholder*="code" i]'
        ).first
        try:
            code_input.wait_for(timeout=15000)
        except Exception:
            page.screenshot(path="no_code_input.png")
            log("检测到验证提示但没找到输入框")
            return False
        code_input.fill(code)
        log("验证码已填")
        page.wait_for_timeout(2000)
        # 点确认
        confirm = page.get_by_role("button", name=re.compile("Continue|Verify", re.I))
        if confirm.count():
            confirm.first.click(timeout=15000)
        page.wait_for_timeout(4000)

    # 6. 确认登录成功
    page.goto(f"{BASE}/createvps", timeout=30000)
    page.wait_for_timeout(2000)
    if "/login" in page.url:
        log("登录失败, 仍在登录页")
        return False
    log("登录成功")
    return True


def check_stock(page):
    """返回 True=有货, False=缺货"""
    page.goto(f"{BASE}/createvps", timeout=30000)
    page.wait_for_timeout(3000)
    text = page.inner_text("body")
    # 找免费套餐区域
    if re.search(r"out of stock", text, re.I):
        # 确认是免费套餐缺货 (页面上可能多个套餐, 简单起见: 有 Deploy Free VPS 可点按钮则算有货)
        deploy_btn = page.locator('button:has-text("Deploy Free VPS"), a:has-text("Deploy Free VPS")').first
        if deploy_btn.count():
            try:
                if deploy_btn.is_enabled():
                    log("发现可点的 Deploy Free VPS 按钮 -> 有货")
                    return True
            except Exception:
                pass
        log("显示 Out of stock -> 缺货")
        return False
    # 没有 Out of stock 字样, 看按钮状态
    deploy_btn = page.locator('button:has-text("Deploy Free VPS"), a:has-text("Deploy Free VPS")').first
    if deploy_btn.count():
        try:
            if deploy_btn.is_enabled():
                log("Deploy 按钮可用 -> 有货")
                return True
        except Exception:
            pass
    log("未发现可领取状态, 视为缺货")
    return False


def claim(page):
    """尝试领取, 返回 (成功, 说明)"""
    log("开始领取流程")
    deploy_btn = page.get_by_role("button", name=re.compile("Deploy Free VPS", re.I))
    if not deploy_btn.count():
        # 可能是链接形式
        deploy_btn = page.locator('a:has-text("Deploy Free VPS")')
    deploy_btn.first.wait_for(timeout=15000)
    deploy_btn.first.click()
    log("已点 Deploy Free VPS")
    page.wait_for_timeout(3000)

    # 选镜像 Debian 13 (如果有这步)
    body = page.inner_text("body")
    if re.search(r"debian", body, re.I):
        deb = page.locator('text=/debian 13/i').first
        if deb.count():
            deb.click()
            log("已选 Debian 13")
            page.wait_for_timeout(1500)

    # Finalize Deployment
    body = page.inner_text("body")
    if re.search(r"captcha|人机验证|verify you are human", body, re.I):
        page.screenshot(path="captcha.png")
        return False, "遇到 CAPTCHA/人机验证, 需人工处理"

    fin = page.get_by_role("button", name=re.compile("Finalize|Confirm", re.I))
    if fin.count():
        fin.first.click(timeout=15000)
        log("已点最终确认")
        page.wait_for_timeout(5000)
    else:
        return False, "没找到确认按钮, 流程可能变了"

    # 检查结果
    page.wait_for_timeout(3000)
    body = page.inner_text("body")
    if re.search(r"captcha|verify you are human", body, re.I):
        page.screenshot(path="captcha.png")
        return False, "最后一步遇到人机验证, 需人工处理"
    # 抓机器信息
    info = body[:2000]
    return True, f"领取流程走完, 页面摘要: {info[:500]}"


def main():
    if not EMAIL or not PASSWORD:
        log("缺少 OPENWORLD_EMAIL / OPENWORLD_PASSWORD, 退出")
        sys.exit(2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        )
        # 恢复 cookie: 优先用 OPENWORLD_COOKIES secret, 其次本地文件
        restored = False
        secret_cookies = os.environ.get("OPENWORLD_COOKIES", "").strip()
        if secret_cookies:
            try:
                cookies = json.loads(secret_cookies)
                ctx.add_cookies(cookies)
                log(f"已从 secret 恢复 {len(cookies)} 个 cookie")
                restored = True
            except Exception as e:
                log(f"secret cookie 恢复失败: {e}")
        if not restored and os.path.exists(COOKIE_FILE):
            try:
                cookies = json.load(open(COOKIE_FILE))
                ctx.add_cookies(cookies)
                log(f"已从文件恢复 {len(cookies)} 个 cookie")
            except Exception as e:
                log(f"cookie 恢复失败: {e}")
        page = ctx.new_page()

        try:
            page.goto(f"{BASE}/createvps", timeout=30000)
            page.wait_for_timeout(2500)
            if "/login" in page.url:
                log("cookie 失效或无 cookie, 需要登录")
                if not do_login(page):
                    page.screenshot(path="login_fail.png")
                    tg("⚠️ Openworld 自动登录失败, 请检查账号或人工登录一次")
                    browser.close()
                    sys.exit(1)
            else:
                log("cookie 有效, 已登录")

            in_stock = check_stock(page)

            # 保存 cookie (无论是否有货都更新): 写文件 + 输出到 stdout 供更新 secret
            try:
                cookies = ctx.cookies()
                with open(COOKIE_FILE, "w") as f:
                    json.dump(cookies, f)
                # 输出标记行, 方便从日志提取更新 secret (cookie 内容 base64, 避免换行问题)
                import base64
                b64 = base64.b64encode(json.dumps(cookies).encode()).decode()
                print(f"COOKIE_UPDATE_B64:{b64}", flush=True)
                log("cookie 已更新保存")
            except Exception as e:
                log(f"cookie 保存失败: {e}")

            if not in_stock:
                log("缺货, 本轮结束")
                browser.close()
                return

            # 有货!
            log("🎯 有货!")
            tg("🎯 Openworld 免费 VPS 有货了! 正在尝试自动领取…")
            if DRY_RUN:
                log("DRY_RUN 模式, 不实际领取")
                browser.close()
                return
            ok, detail = claim(page)
            if ok:
                tg(f"✅ 领取成功!\n{detail}")
                log("领取成功")
            else:
                page.screenshot(path="claim_fail.png")
                tg(f"⚠️ 有货但自动领取被挡住: {detail}\n请尽快人工去抢: {BASE}/createvps")
                log(f"领取被挡: {detail}")
        except PWTimeout as e:
            page.screenshot(path="timeout.png")
            tg(f"⚠️ Openworld 监控脚本超时: {e}")
            log(f"超时: {e}")
            sys.exit(1)
        except Exception as e:
            try:
                page.screenshot(path="error.png")
            except Exception:
                pass
            tg(f"⚠️ Openworld 监控脚本出错: {e}")
            log(f"出错: {e}")
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
