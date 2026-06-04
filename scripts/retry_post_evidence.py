"""Re-trigger ERP `post_evidence_to_marketplace` for one or more G2G/Eldo orders.

Use when an order is already Completed on the marketplace and on ERP, but the
evidence file was never accepted by the marketplace (worker failure at the time).
The Sell Order's workflow is already past `Deliver`, so the bot must skip the
qty step — the script always passes `skip_steps=['qty']`.

Usage (from Windows host):
    python scripts/retry_post_evidence.py <order_id> [<order_id> ...]

The script:
  1. SSH to ERP (192.168.2.100) — looks up Sell Order name for each external_order_id
  2. Tails the active G2G + Eldo worker log on the bot server (192.168.2.220)
  3. Calls `post_evidence_to_marketplace(SO, skip_steps='[\"qty\"]')` per order
  4. Waits up to 90s per order for `Completed: <order_id>` / `Task error for <order_id>`
  5. Prints per-order verdict + final summary

Notes:
  - `post_evidence_to_marketplace` raises `WorkflowTransitionError: Not a valid Workflow Action`
    AFTER the worker call succeeds (because the SO is already in a terminal state).
    That exception is benign — the proof was sent before it fires.
  - Verify on the marketplace dashboard: open the order → Delivery/Evidence tab →
    the file from ERP Order Evidence record should be listed.
"""
import argparse
import re
import sys
import threading
import time

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ERP_HOST = "192.168.2.100"
ERP_USER = "root"
ERP_PASS = "123456"
BOT_HOST = "192.168.2.220"
BOT_USER = "root"
BOT_PASS = "123456"


def ssh_connect(host, user, pwd):
    s = paramiko.SSHClient()
    s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    s.connect(host, username=user, password=pwd, timeout=15)
    return s


def run(ssh, cmd, timeout=60):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    return (stdout.read().decode(errors="replace"),
            stderr.read().decode(errors="replace"))


def active_worker_log(ssh, pattern):
    """Return path of /tmp log file the worker python process writes to.
    Falls back to the newest /tmp/<pattern>*.log."""
    pid, _ = run(ssh, f"pgrep -f 'python.*{pattern}'")
    pid = pid.strip().splitlines()[0] if pid.strip() else ""
    if pid:
        out, _ = run(ssh, f"readlink /proc/{pid}/fd/1 2>/dev/null")
        out = out.strip()
        if out.startswith("/tmp/"):
            return out
    out, _ = run(ssh, f"ls -t /tmp/{pattern}*.log 2>/dev/null | head -1")
    return out.strip() or f"/tmp/{pattern}.log"


def lookup_sell_orders(erp_ssh, order_ids):
    """Look up SO name + sell_channel for each external_order_id.
    Returns dict {order_id: {'so': str, 'channel': str, 'state': str}} or None per id."""
    sftp = erp_ssh.open_sftp()
    script_lines = [
        'import frappe, json',
        'frappe.init(site="erp.gegeteam.net")',
        'frappe.connect()',
        'frappe.set_user("Administrator")',
        'ids = ' + repr(list(order_ids)),
        'rows = frappe.get_all("Sell Order",',
        '    filters={"external_order_id": ["in", ids]},',
        '    fields=["name","external_order_id","workflow_state","sell_channel"])',
        'print("LOOKUP:" + json.dumps(rows, default=str))',
    ]
    import uuid
    lookup_path = f"/tmp/_retry_lookup_{uuid.uuid4().hex[:10]}.py"
    with sftp.open(lookup_path, "w") as f:
        f.write("\n".join(script_lines))
    sftp.close()
    run(erp_ssh, f"chmod 644 {lookup_path}")

    out, err = run(
        erp_ssh,
        "sudo -u frappe bash -lc "
        f"'cd /home/frappe/frappe-bench/sites && ../env/bin/python {lookup_path}'",
        timeout=60,
    )
    result = {oid: None for oid in order_ids}
    for line in out.splitlines():
        if line.startswith("LOOKUP:"):
            import json
            rows = json.loads(line[len("LOOKUP:"):])
            for r in rows:
                result[r["external_order_id"]] = {
                    "so": r["name"],
                    "channel": (r.get("sell_channel") or ""),
                    "state": r.get("workflow_state") or "",
                }
            break
    if err and "Traceback" in err:
        print(f"[lookup stderr]\n{err[:1500]}")
    return result


def call_post_evidence(erp_ssh, so_name):
    """Run `post_evidence_to_marketplace(so, skip_steps='[\"qty\"]')` via bench env python.
    The function raises WorkflowTransitionError AFTER worker has accepted — treat that
    specific exception as benign. Returns (worker_accepted_bool, raw_output_str)."""
    # Use a unique path per call to avoid permission conflicts when frappe owns
    # the previous file. Cleanup happens via tmpwatch / reboot.
    import uuid
    script_path = f"/tmp/_retry_call_{uuid.uuid4().hex[:10]}.py"
    sftp = erp_ssh.open_sftp()
    body = (
        'import frappe, sys, traceback\n'
        'frappe.init(site="erp.gegeteam.net")\n'
        'frappe.connect()\n'
        'frappe.set_user("Administrator")\n'
        'from gege_custom.gege_custom.api.botpastedon import post_evidence_to_marketplace\n'
        'try:\n'
        f'    r = post_evidence_to_marketplace(sell_order_name={so_name!r}, skip_steps=\'["qty"]\')\n'
        '    print("OK:", r)\n'
        'except Exception as e:\n'
        '    msg = str(e)\n'
        '    if "Not a valid Workflow Action" in msg:\n'
        '        print("BENIGN_WORKFLOW_ERROR")\n'
        '    else:\n'
        '        traceback.print_exc()\n'
        '        print("EXCEPTION:", msg)\n'
        'finally:\n'
        '    frappe.destroy()\n'
    )
    with sftp.open(script_path, "w") as f:
        f.write(body)
    sftp.close()
    run(erp_ssh, f"chmod 644 {script_path}")

    out, err = run(
        erp_ssh,
        "sudo -u frappe bash -lc "
        f"'cd /home/frappe/frappe-bench/sites && ../env/bin/python {script_path}'",
        timeout=120,
    )
    raw = (out + ("\n[stderr]" + err if err.strip() else "")).strip()
    accepted = ("OK:" in out) or ("BENIGN_WORKFLOW_ERROR" in out)
    return accepted, raw


def tail_thread(ssh, log_paths, buf, stop_flag):
    """Tail multiple log files concurrently — append every line to buf."""
    cmd = "tail -n 0 -F " + " ".join(log_paths)
    chan = ssh.get_transport().open_session()
    chan.exec_command(cmd)
    chan.settimeout(1.0)
    while not stop_flag[0]:
        try:
            data = chan.recv(4096)
            if data:
                buf.append(data.decode(errors="replace"))
        except Exception:
            time.sleep(0.2)
    chan.close()


def wait_for_terminal(order_id, buf, timeout_sec=90):
    """Wait until the order log shows a Completed / Task error / fatal line.
    Returns one of: 'completed', 'failed', 'timeout', and the matching line."""
    completed_re = re.compile(rf"Completed:\s*{re.escape(order_id)}\b")
    error_re = re.compile(rf"Task error for {re.escape(order_id)}\b")
    jwt_warn_re = re.compile(rf"Auth error for {re.escape(order_id)}\b")
    deadline = time.time() + timeout_sec
    last_seen = ""
    while time.time() < deadline:
        text = "".join(buf)
        for line in text.splitlines()[-300:]:
            if completed_re.search(line):
                return "completed", line.strip()
            if error_re.search(line):
                return "failed", line.strip()
            if jwt_warn_re.search(line):
                last_seen = line.strip()
        time.sleep(1)
    return "timeout", last_seen


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("order_ids", nargs="+", help="One or more external_order_id values")
    parser.add_argument("--per-order-timeout", type=int, default=90,
                        help="Seconds to wait per order for terminal log line (default 90)")
    args = parser.parse_args()

    order_ids = args.order_ids
    print(f"Processing {len(order_ids)} order(s).\n")

    erp = ssh_connect(ERP_HOST, ERP_USER, ERP_PASS)
    bot = ssh_connect(BOT_HOST, BOT_USER, BOT_PASS)

    # Find which log files the live workers write to
    g2g_log = active_worker_log(bot, "g2g_worker")
    eldo_log = active_worker_log(bot, "eldo_worker")
    print(f"Tailing {g2g_log}")
    print(f"Tailing {eldo_log}\n")

    buf = []
    stop_flag = [False]
    tail = threading.Thread(
        target=tail_thread, args=(bot, [g2g_log, eldo_log], buf, stop_flag), daemon=True,
    )
    tail.start()
    time.sleep(1.5)

    # 1. Look up SO names
    print(">>> Looking up Sell Orders ...")
    so_map = lookup_sell_orders(erp, order_ids)

    summary = []
    for oid in order_ids:
        info = so_map.get(oid)
        if not info:
            print(f"\n[{oid}] NO Sell Order found in ERP — skipped")
            summary.append((oid, "-", "no_so"))
            continue

        so_name = info["so"]
        channel = info["channel"]
        state = info["state"]
        print(f"\n[{oid}] SO={so_name} channel={channel} state={state}")

        if state == "Cancelled":
            print(f"  -> Cancelled. Not re-triggering.")
            summary.append((oid, so_name, "cancelled"))
            continue

        accepted, raw = call_post_evidence(erp, so_name)
        if not accepted:
            short = " | ".join(l for l in raw.splitlines() if l)[:300]
            print(f"  -> ERP call did NOT report worker-accepted. raw: {short}")
            summary.append((oid, so_name, "erp_fail"))
            continue

        print(f"  -> ERP call returned; waiting up to {args.per_order_timeout}s for worker...")
        verdict, line = wait_for_terminal(oid, buf, timeout_sec=args.per_order_timeout)
        print(f"  -> {verdict.upper()}: {line or '(no terminal line found)'}")
        summary.append((oid, so_name, verdict))

    stop_flag[0] = True
    time.sleep(1.5)
    bot.close()
    erp.close()

    print("\n" + "=" * 72)
    print(f"{'Order ID':<22} {'SO':<22} Verdict")
    print("-" * 72)
    for oid, so, verdict in summary:
        print(f"{oid:<22} {so:<22} {verdict}")
    print("=" * 72)
    succ = sum(1 for _, _, v in summary if v == "completed")
    print(f"\n{succ}/{len(summary)} orders completed by worker.")
    print("→ Verify each on the G2G dashboard (Delivery/Evidence tab).")


if __name__ == "__main__":
    main()
