#!/usr/bin/env python3
# 用 node_proxy.py 把 NODE_LINK 转成本地 http 代理，sing-box 后台常驻到 job 结束，
# 并把 LOCAL_PROXY / PROXY_IP 写进 GITHUB_ENV 供后续步骤使用。
import contextlib, io, json, os, socket, subprocess, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import node_proxy as np

link = os.environ.get("NODE_LINK", "").strip()
if not link:
    print("未配置 NODE_LINK，直连")
    sys.exit(0)

workdir = os.environ.get("RUNNER_TEMP", "/tmp")
with contextlib.redirect_stdout(io.StringIO()):  # 公开仓库：不打印节点域名/SNI
    scheme, info = np.normalize_link(link)
print("节点协议：%s" % scheme)
ob = np.build_outbound(scheme, info)
bindir = os.path.expanduser("~/.cache/sing-box-%s" % np.SING_BOX_VERSION)  # 由 actions/cache 缓存
os.makedirs(bindir, exist_ok=True)
binpath = np.ensure_singbox(bindir)
port = np._free_port()
proxy = "http://127.0.0.1:%d" % port
cfg = {"log": {"level": "warning"},
       "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": port}],
       "outbounds": [ob], "route": {"final": "proxy"}}
cfgpath = os.path.join(workdir, "sb-config.json")
fd = os.open(cfgpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f)
r = subprocess.run([binpath, "check", "-c", cfgpath], capture_output=True, text=True, timeout=30)
if r.returncode != 0:
    sys.exit("sing-box 配置校验失败：%s" % (r.stderr or r.stdout)[:300])
logpath = os.path.join(workdir, "sing-box.log")
proc = subprocess.Popen([binpath, "run", "-c", cfgpath], stdout=open(logpath, "w"),
                        stderr=subprocess.STDOUT, start_new_session=True)
for _ in range(30):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        break
    except OSError:
        if proc.poll() is not None:
            sys.exit("sing-box 启动后异常退出：%s" % np._read_log_tail(logpath))
        time.sleep(1)
else:
    sys.exit("sing-box 30s 内未就绪")
ip = np.probe_exit_ip(proxy)
if not ip:
    sys.exit("代理自检失败：%s" % np._read_log_tail(logpath))
print("代理就绪，出口 IP：%s" % ip)
with open(os.environ["GITHUB_ENV"], "a") as f:
    f.write("LOCAL_PROXY=%s\nPROXY_IP=%s\n" % (proxy, ip))
