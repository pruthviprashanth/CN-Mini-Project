"""Helpers shared by the experiment runner and the Mininet integration tests."""
import json
import os
import subprocess
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REST = "http://127.0.0.1:8080"
EVENT_SOCK = "/tmp/chat_sdn_events.sock"
# Ryu needs Python 3.9 + eventlet 0.30.2; see README "VM setup".
RYU = os.environ.get("RYU_MANAGER", os.path.expanduser("~%s/ryu-venv/bin/ryu-manager"
                                                       % os.environ.get("SUDO_USER", "")))


def cleanup_mininet():
    # Must run before the controller starts: `mn -c` also kills ryu-manager.
    subprocess.call(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_controller(log_path, mode="sdn", monitor=True, policy=None, extra_env=None):
    env = dict(os.environ, CHAT_MODE=mode, CHAT_MONITOR="1" if monitor else "0")
    if policy:
        env["CHAT_POLICY"] = policy
    env.update(extra_env or {})
    logf = open(log_path, "w")
    proc = subprocess.Popen([RYU, os.path.join(ROOT, "sdn", "controller.py")], cwd=ROOT,
                            env=env, stdout=logf, stderr=subprocess.STDOUT)
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("controller exited; see %s" % log_path)
        try:
            rest("GET", "/health")
            return proc
        except OSError:
            time.sleep(0.3)
    proc.kill()
    raise RuntimeError("controller REST did not come up; see %s" % log_path)


def stop_controller(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
    # Started as root here, so remove the socket or a later non-root controller
    # cannot bind it (/tmp is sticky).
    try:
        os.unlink(EVENT_SOCK)
    except OSError:
        pass


def rest(method, path, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(REST + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def wait_switches(n=2, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(rest("GET", "/health")["datapaths"]) >= n:
            return
        time.sleep(0.2)
    raise RuntimeError("switches did not connect to the controller")


def dump_flows(switch):
    return subprocess.check_output(["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", switch]).decode()


def tc_classes(dev):
    """{queue_id: bytes sent} from `tc -s class show`; OVS maps queue q to class 1:(q+1)."""
    out = subprocess.check_output(["tc", "-s", "class", "show", "dev", dev]).decode()
    res, cur = {}, None
    for line in out.splitlines():
        parts = line.split()
        if parts[:2] == ["class", "htb"]:
            minor = int(parts[2].split(":")[1], 16)
            # 1:fffe is the HTB root; leaves are 1:1 (queue 0, default) .. 1:4.
            cur = minor - 1 if minor < 0xfff0 else None
        elif parts[:1] == ["Sent"] and cur is not None:
            res[cur] = int(parts[1])
            cur = None
    return res
