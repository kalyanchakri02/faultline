import re
from datetime import datetime, timedelta

def parse(text: str):
    lines = text.strip().splitlines()
    result = []
    # regex for syslog timestamp: Mmm dd hh:mm:ss
    syslog_re = re.compile(r'^(\w{3})\s+(\d{1,2})\s+(\d{2}:\d{2}:\d{2})')
    # months map
    months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
              'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
    for line in lines:
        m = syslog_re.match(line)
        if not m:
            continue
        mon_str, day_str, time_str = m.groups()
        month = months.get(mon_str)
        if month is None:
            continue
        day = int(day_str)
        hour, minute, second = map(int, time_str.split(':'))
        # Assume year 2026 (could be previous year if date wraps, but fine)
        year = 2026
        try:
            dt = datetime(year, month, day, hour, minute, second)
        except ValueError:
            continue
        iso_ts = dt.isoformat()
        # Remove timestamp and host part: after third space? Simpler: strip prefix up to first ': ' after time?
        # We'll just keep the message after the hostname: find first ':' after time?
        # Simpler: split line into parts, take everything after the third whitespace group.
        parts = line.split()
        # parts[0]=Mon, parts[1]=day, parts[2]=time, parts[3]=host, rest=message
        if len(parts) < 4:
            continue
        msg = ' '.join(parts[4:])
        # Determine kind
        xid = None
        pid = None
        pci = None
        kind = None
        if 'NVRM: Xid' in msg:
            kind = 'xid'
            # Extract Xid number after PCI:xxxx
            # pattern: NVRM: Xid (PCI:....): <code>, ...
            mxid = re.search(r'NVRM: Xid \(PCI:[^)]+\):\s*(\d+)', msg)
            if mxid:
                xid = int(mxid.group(1))
            # pid
            mp = re.search(r'pid=([^\s,]+)', msg)
            if mp:
                pid_str = mp.group(1)
                if pid_str.isdigit():
                    pid = int(pid_str)
                else:
                    pid = None
            # pci
            mpci = re.search(r'PCI:([0-9a-fA-F:]+)', msg)
            if mpci:
                pci = mpci.group(1)
        elif 'AER:' in msg:
            kind = 'aer'
        elif 'clocks throttled' in msg:
            kind = 'throttle'
        else:
            continue
        result.append({
            'ts': iso_ts,
            'kind': kind,
            'xid': xid,
            'pid': pid,
            'pci': pci,
            'msg': msg
        })
    return result