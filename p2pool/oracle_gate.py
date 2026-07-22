"""
Oracle-gate instrumentation for the p2pool-dash diagnostic node.

All behavior here is OFF by default and only activates when the environment
variable P2POOL_ORACLE_GATE=1 is set. When disabled, is_enabled() returns
False and none of the gated call sites change the node's behavior, so the
node runs byte-identical to vanilla.

Environment variables
---------------------
P2POOL_ORACLE_GATE=1
    Master switch. Turns on verbose logging, the expected-vs-received byte
    diff on share-verify failure, and the log-instead-of-ban behavior.

P2POOL_ORACLE_LOG=/path/to/oracle-gate.log
    Destination log file. Defaults to ~/oracle-gate.log

P2POOL_GATE_NOBAN_IPS=ip1,ip2,...
    Comma-separated allowlist of peer IPs that must never be banned or
    dropped for a bad/unrecognized share or protocol quirk. When this is
    set, ONLY these IPs are protected; all other peers are treated normally.

P2POOL_GATE_NOBAN_ALL=1
    Never ban/drop ANY peer for a bad share or protocol quirk. This is the
    default when the gate is on and no explicit allowlist is provided, since
    the whole point of this node is to let peers iterate without being
    banned. Set P2POOL_GATE_NOBAN_IPS to narrow the exemption instead.

The log is tab-separated and greppable. Each line:
    <iso-timestamp>\t<epoch>\t<CATEGORY>\tkey=value\tkey=value ...
Grep by category, e.g.:  grep '\tSHARE_RECV\t' ~/oracle-gate.log
"""

import os
import sys
import time
import traceback


def _truthy(val):
    return val is not None and val.strip().lower() in ('1', 'true', 'yes', 'on')


ENABLED = _truthy(os.environ.get('P2POOL_ORACLE_GATE'))

_LOG_PATH = os.environ.get('P2POOL_ORACLE_LOG') or os.path.expanduser('~/oracle-gate.log')

_NOBAN_IPS = set(
    ip.strip() for ip in os.environ.get('P2POOL_GATE_NOBAN_IPS', '').split(',') if ip.strip()
)

# Global no-ban is explicit via P2POOL_GATE_NOBAN_ALL, or implicit when the
# gate is on but no allowlist was supplied (permissive controlled node).
_NOBAN_ALL = _truthy(os.environ.get('P2POOL_GATE_NOBAN_ALL')) or (ENABLED and not _NOBAN_IPS)

_log_file = None


def is_enabled():
    return ENABLED


def noban(ip):
    """Return True if this peer IP must NOT be banned/dropped for a bad share."""
    if not ENABLED:
        return False
    if _NOBAN_ALL:
        return True
    return ip in _NOBAN_IPS


def _get_file():
    global _log_file
    if _log_file is None:
        try:
            _log_file = open(_LOG_PATH, 'a', 0)  # unbuffered append
        except Exception:
            traceback.print_exc()
            _log_file = False  # mark as failed so we don't retry every line
    return _log_file or None


def _fmt_field(key, value):
    # Keep values on one greppable line: no tabs/newlines.
    s = str(value)
    s = s.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')
    return '%s=%s' % (key, s)


def log(category, **fields):
    """Write one structured line to the oracle-gate log. Never raises."""
    if not ENABLED:
        return
    try:
        now = time.time()
        parts = [time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now)), '%.3f' % now, category]
        # Deterministic-ish ordering: sorted keys, but keep it cheap.
        for key in sorted(fields):
            parts.append(_fmt_field(key, fields[key]))
        line = '\t'.join(parts) + '\n'
        f = _get_file()
        if f is not None:
            f.write(line)
        else:
            sys.stderr.write('ORACLE-GATE ' + line)
    except Exception:
        try:
            traceback.print_exc()
        except Exception:
            pass


def hexify(data):
    """Hex-encode raw bytes for KAT vectors. Never raises."""
    try:
        return data.encode('hex')
    except Exception:
        return '<unhexable:%r>' % (type(data),)
