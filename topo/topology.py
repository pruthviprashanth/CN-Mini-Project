#!/usr/bin/env python3
"""Mininet topology: clients + bulk host on s1, chat server + sink on s2,
joined by one shaped trunk link. Needs root and a running controller.

  c1..cN, bulk -- s1 ==== 10 Mbps trunk ==== s2 -- srv (10.0.0.1), sink (10.0.0.2)

Usage:
  sudo python3 topo/topology.py demo     # topology + chat server on srv + CLI
  sudo python3 topo/topology.py cli      # topology + CLI only
  sudo python3 topo/topology.py check    # build, pingall, show QoS, exit
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

from mininet.cli import CLI
from mininet.link import Link
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_IP, SINK_IP, BULK_IP = "10.0.0.1", "10.0.0.2", "10.0.0.100"
# Per-queue (min-rate share of the port, HTB priority); queue ids match sdn/policy.py.
QUEUE_SHARES = {1: (0.20, 1), 2: (0.50, 2), 3: (0.10, 3)}


def client_ip(i):  # IP of client i: 10.0.0.(10+i)
    return "10.0.0.%d" % (10 + i)


def build_network(n_clients=4, trunk_mbps=10, access_mbps=100, txqueuelen=100,
                  controller_ip="127.0.0.1", controller_port=6653, qos=True):  # create switches, hosts, links, rate limits and queues; returns the Mininet network
    """Build and start the network; returns the Mininet object."""
    net = Mininet(controller=None, switch=OVSSwitch, link=Link, autoSetMacs=True, build=False)
    net.addController("c0", controller=RemoteController, ip=controller_ip, port=controller_port)
    s1 = net.addSwitch("s1", protocols="OpenFlow13", failMode="secure", dpid="1")
    s2 = net.addSwitch("s2", protocols="OpenFlow13", failMode="secure", dpid="2")
    # Link order fixes port numbers: s1-eth1..N clients, then bulk, then trunk;
    # s2-eth1 srv, s2-eth2 sink, s2-eth3 trunk.
    for i in range(1, n_clients + 1):
        net.addLink(net.addHost("c%d" % i, ip=client_ip(i) + "/24"), s1)
    net.addLink(net.addHost("bulk", ip=BULK_IP + "/24"), s1)
    net.addLink(net.addHost("srv", ip=SERVER_IP + "/24"), s2)
    net.addLink(net.addHost("sink", ip=SINK_IP + "/24"), s2)
    net.addLink(s1, s2)
    net.build()
    net.start()
    trunk = {intf.name for link in net.linksBetween(s1, s2) for intf in (link.intf1, link.intf2)}
    for sw in (s1, s2):
        for intf in sw.intfList():
            if intf.name == "lo":
                continue
            rate = (trunk_mbps if intf.name in trunk else access_mbps) * 1000000
            _tune(intf.name, txqueuelen)
            if qos:
                _configure_qos(intf.name, rate)
    net.trunk_intfs = sorted(trunk)
    return net


def _tune(dev, txqueuelen):  # set the interface queue length and turn off segmentation offloads
    # The default txqueuelen of 1000 adds ~1 s of queueing at 10 Mbps.
    subprocess.check_call(["ip", "link", "set", "dev", dev, "txqueuelen", str(txqueuelen)])
    # Segmentation offload would hand HTB 64 KB super-packets; shaping and
    # queueing delay are only realistic with MTU-sized packets.
    if shutil.which("ethtool"):
        subprocess.call(["ethtool", "-K", dev, "tso", "off", "gso", "off", "gro", "off"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _configure_qos(port, rate):  # create the HTB QoS with queues 1-3 on a switch port via ovs-vsctl
    cmd = ["ovs-vsctl", "--", "set", "Port", port, "qos=@q", "--", "--id=@q", "create", "QoS",
           "type=linux-htb", "other-config:max-rate=%d" % rate]
    cmd += ["queues:%d=@q%d" % (q, q) for q in sorted(QUEUE_SHARES)]
    for q, (share, prio) in sorted(QUEUE_SHARES.items()):
        cmd += ["--", "--id=@q%d" % q, "create", "Queue",
                "other-config:min-rate=%d" % int(rate * share),
                "other-config:max-rate=%d" % rate, "other-config:priority=%d" % prio]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL)


def stop_network(net):  # stop the network and delete the QoS/Queue records
    net.stop()
    # Ports are gone now, so the QoS/Queue rows are unreferenced.
    subprocess.call(["ovs-vsctl", "--all", "destroy", "QoS", "--", "--all", "destroy", "Queue"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_server(net, users_file="/tmp/chat_users.json", log="/tmp/chat_server.log",
                 controller="unix:/tmp/chat_sdn_events.sock", fresh=True):  # start the chat server inside host srv and wait until it is listening
    if fresh and os.path.exists(users_file):
        os.unlink(users_file)
    srv = net.get("srv")
    logf = open(log, "w")
    proc = srv.popen(["python3", "-u", os.path.join(ROOT, "chat", "server.py"),
                      "--users-file", users_file, "--controller", controller],
                     cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT)
    for _ in range(50):
        if "listening" in open(log).read():
            break
        time.sleep(0.1)
    return proc


def show_qos(net):  # print the tc queue counters of the trunk ports
    for dev in net.trunk_intfs:
        print("--- tc classes on %s" % dev)
        print(subprocess.check_output(["tc", "-s", "class", "show", "dev", dev]).decode())


def main():  # parse options, build the network, start the server, ping all, open the CLI
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["demo", "cli", "check"], nargs="?", default="demo")
    p.add_argument("--clients", type=int, default=4)
    p.add_argument("--trunk-mbps", type=float, default=10)
    p.add_argument("--no-qos", action="store_true")
    args = p.parse_args()
    setLogLevel("info")
    net = build_network(args.clients, args.trunk_mbps, qos=not args.no_qos)
    server = None
    try:
        info("*** waiting for switches to connect\n")
        net.waitConnected(timeout=10)
        if args.action == "demo":
            server = start_server(net)
            info("*** chat server running on srv %s:5000 (log /tmp/chat_server.log)\n" % SERVER_IP)
        loss = net.pingAll()
        if args.action == "check":
            show_qos(net)
            sys.exit(0 if loss == 0 else 1)
        if args.action == "demo":
            info("*** try:  c1 python3 chat/client.py --user vip_alice --password pw1234 --register\n")
        CLI(net)
    finally:
        if server:
            server.terminate()
        stop_network(net)


if __name__ == "__main__":
    main()
