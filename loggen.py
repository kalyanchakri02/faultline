"""Synthetic GPU-node incident generator (follows NVIDIA's public Xid docs). Returns files + ground truth."""
import json, random, re, datetime as dt

SCENARIOS = {
    "ecc_remap_exhausted": dict(title="Uncorrectable ECC, row-remap exhausted", classification="memory", action="rma"),
    "fell_off_bus": dict(title="GPU fell off the bus", classification="pcie", action="drain_reboot"),
    "nvlink_flaky": dict(title="Flaky NVLink on link 5", classification="interconnect", action="gpu_reset"),
    "app_xid13": dict(title="Noisy but harmless application error", classification="application", action="investigate_workload"),
    "healthy_node": dict(title="Healthy node (alert was a false positive)", classification="none", action="monitor"),
    "sbe_creep_no_xid": dict(title="Correctable ECC creeping up, no Xid", classification="memory", action="monitor"),
    "thermal_throttle": dict(title="Thermal throttling, no Xid", classification="thermal", action="inspect_cooling"),
}
# (log format, telemetry format) pairs: the agent has never seen these formats until it meets them
FORMATS = [("syslog", "csv"), ("journal_iso", "dcgm_jsonl"), ("journal_json", "dmon")]

def _ts(t): return t.strftime("%b %d %H:%M:%S")

def generate(name: str, seed: int = 7, fmt_id: int = 0, xid_style: str = "v1"):
    r = random.Random(f"{name}-{seed}")
    t0 = dt.datetime(2026, 10, 20, 2, 0, 0)
    host = f"gpu-node-{r.randint(100,999)}"
    gpu = r.randint(0, 7)
    pci = f"0000:{r.choice(['1b','3d','5e','86','a9','bb'])}:00"
    log, rows = [], []
    sbe = dbe = remap = rfail = crc = replay = 0
    temp, power, sm = 62.0, 380.0, 1980
    trows = []
    events = {}  # minute -> list of log lines

    def noise(m):
        t = t0 + dt.timedelta(minutes=m, seconds=r.randint(0, 59))
        return r.choice([
            f"{_ts(t)} {host} kernel: nvidia-nvswitch0: SXid warning rate-limited (benign)",
            f"{_ts(t)} {host} kernel: [UVM] GPU{r.randint(0,7)} p2p mapping refreshed",
            f"{_ts(t)} {host} systemd[1]: Started Session {r.randint(100,999)} of user slurm.",
            f"{_ts(t)} {host} kernel: NVRM: GPU at PCI:{pci}: GPU-{r.randint(1000,9999)} clocks throttled by SW power cap",
            f"{_ts(t)} {host} nvidia-persistenced: device {pci} - persistence mode enabled",
        ])

    def xid(m, g, code, msg, extra=""):
        t = t0 + dt.timedelta(minutes=m, seconds=r.randint(0, 59))
        return f"{_ts(t)} {host} kernel: NVRM: Xid (PCI:{pci}): {code}, pid={extra or r.randint(2000,9000)}, name=python, {msg}"

    for m in range(0, 360):
        if r.random() < 0.18: log.append(noise(m))
        temp += r.uniform(-0.6, 0.6); power += r.uniform(-8, 8); sm = 1980
        if name == "sbe_creep_no_xid":
            if m > 60 and r.random() < 0.05 + m / 2500: sbe += r.randint(1, 3)
        elif name == "thermal_throttle" and m > 150:
            temp = min(temp + 0.12, 91.0); sm = int(1980 - (temp - 70) * 40) if temp > 70 else 1980
            if m % 7 == 0: log.append(f"{_ts(t0+dt.timedelta(minutes=m))} {host} kernel: NVRM: GPU at PCI:{pci}: clocks throttled by HW slowdown (thermal), temperature {temp:.0f}C")
        if name == "ecc_remap_exhausted":
            if m > 120 and r.random() < 0.06: sbe += r.randint(1, 4)
            if m in (200, 230, 260, 280):
                dbe += 1; log.append(xid(m, gpu, 94, "Contained: CE/DBE ECC error in framebuffer, row remap pending"))
                log.append(xid(m, gpu, 63, f"Row remapper: new row remap recorded (bank {r.randint(0,15)})")); remap += 1
            if m in (300, 330):
                rfail += 1; log.append(xid(m, gpu, 64, "Row remapper: row remap failure, no spare rows available (remapping resources exhausted)"))
        elif name == "fell_off_bus":
            if 150 < m < 175 and r.random() < 0.3:
                replay += r.randint(1, 5)
                log.append(f"{_ts(t0+dt.timedelta(minutes=m))} {host} kernel: pcieport {pci}: AER: Corrected error received: Replay Timer Timeout")
            if m == 180:
                log.append(f"{_ts(t0+dt.timedelta(minutes=m))} {host} kernel: pcieport {pci}: AER: Uncorrected (Fatal) error received: Surprise Down")
                log.append(xid(m, gpu, 79, "GPU has fallen off the bus."))
                log.append(f"{_ts(t0+dt.timedelta(minutes=m,seconds=2))} {host} kernel: NVRM: GPU {pci}: GPU is lost, and nvidia-smi can no longer see it")
        elif name == "nvlink_flaky":
            if m > 100: crc += int(r.random() < 0.5) * r.randint(5, 60) * (1 + m // 120)
            if m > 100 and r.random() < 0.05:
                log.append(xid(m, gpu, 74, f"NVLink: fatal error detected on link 5 (0x{r.randint(0x100,0xfff):x}, 0x0, 0x0, CRC)"))
        elif name == "app_xid13":
            if r.random() < 0.04 and m > 30:
                log.append(xid(m, gpu, 13, f"Graphics Exception: ESR 0x{r.randint(0x400000,0x4fffff):x}=0x{r.randint(1,9)}0000", extra="4417"))
                log.append(f"{_ts(t0+dt.timedelta(minutes=m,seconds=1))} {host} train.py[4417]: CUDA error: an illegal memory access was encountered")
        if m % 5 == 0:
            trows.append(dict(ts=(t0+dt.timedelta(minutes=m)), temp=round(temp, 1), power=round(power), sm=sm, sbe=sbe, dbe=dbe,
                              remap=remap, rfail=rfail, crc=crc, replay=replay))

    if xid_style != "v1": log = [_restyle(l, xid_style) for l in log]
    log.sort(key=lambda s: dt.datetime.strptime("2026 " + s[:15], "%Y %b %d %H:%M:%S"))
    log_fmt, telem_fmt = FORMATS[fmt_id % len(FORMATS)]
    return {
        "scenario": name, "host": host, "gpu": gpu, "fmt": {"log": log_fmt, "telemetry": telem_fmt},
        "files": {"dmesg.log": render_log(log, log_fmt), "dcgm_telemetry.txt": render_telemetry(trows, gpu, telem_fmt)},
        "truth": SCENARIOS[name],
        "alert": f"Alert on {host} GPU{gpu} ({pci}): job failures / GPU health check failed. Files: dmesg.log (kernel/system log), dcgm_telemetry.txt (5-min DCGM samples). Formats are not guaranteed.",
    }

_XID_V1 = re.compile(r"NVRM: Xid \(PCI:([^)]*)\): (\d+), pid=([^,]+), name=([^,]+), (.*)")

def _restyle(line, style):
    """HYPOTHETICAL future Xid wordings, used only to test format drift (not real NVIDIA formats)."""
    m = _XID_V1.search(line)
    if not m: return line
    pci, code, pid, name, msg = m.groups(); head = line[:m.start()]
    if style == "v2": return f"{head}NVRM: GPU {pci} reported Xid {code} (pid {pid}, {name}) - {msg}"
    return f"{head}NVRM: Xid error code={code} dev={pci} pid={pid} proc={name}: {msg}"

_SYSLOG = re.compile(r"^(\w{3} +\d+ \d\d:\d\d:\d\d) (\S+) ([^:]+): (.*)$")

def render_log(lines, fmt):
    if fmt == "syslog": return "\n".join(lines) + "\n"
    out = []
    for ln in lines:
        m = _SYSLOG.match(ln); ts, host, ident, msg = m.groups()
        t = dt.datetime.strptime("2026 " + ts, "%Y %b %d %H:%M:%S")
        if fmt == "journal_iso":
            out.append(f"{t.strftime('%Y-%m-%dT%H:%M:%S')}+0000 {host} {ident}: {msg}")
        else:
            out.append(json.dumps({"__REALTIME_TIMESTAMP": str(int(t.replace(tzinfo=dt.timezone.utc).timestamp() * 1e6)), "_HOSTNAME": host,
                                   "SYSLOG_IDENTIFIER": ident, "PRIORITY": "3", "MESSAGE": msg}))
    return "\n".join(out) + "\n"

def render_telemetry(rows, gpu, fmt):
    if fmt == "csv":
        h = "timestamp,gpu,temp_c,power_w,sm_clock_mhz,ecc_sbe_total,ecc_dbe_total,remapped_rows,remap_failures,nvlink5_crc_errors,pcie_replay_count"
        return h + "\n" + "\n".join(f"{x['ts'].isoformat()},{gpu},{x['temp']},{x['power']},{x['sm']},{x['sbe']},{x['dbe']},{x['remap']},{x['rfail']},{x['crc']},{x['replay']}" for x in rows) + "\n"
    if fmt == "dcgm_jsonl":
        return "\n".join(json.dumps({"time": int(x["ts"].replace(tzinfo=dt.timezone.utc).timestamp()), "gpu_id": gpu,
            "DCGM_FI_DEV_GPU_TEMP": x["temp"], "DCGM_FI_DEV_POWER_USAGE": x["power"], "DCGM_FI_DEV_SM_CLOCK": x["sm"],
            "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL": x["sbe"], "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL": x["dbe"],
            "DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS": x["remap"], "DCGM_FI_DEV_ROW_REMAP_FAILURE": x["rfail"],
            "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_L5": x["crc"], "DCGM_FI_DEV_PCIE_REPLAY_COUNTER": x["replay"]}) for x in rows) + "\n"
    hdr = ("#Time                 GPU  TEMP  POWER  SMCLK  SBE  DBE  REMAP  RFAIL  NVL5CRC  PCIEREPLAY\n"
           "#ISO8601              id   C     W      MHz    cnt  cnt  rows   cnt    cnt      cnt\n")
    return hdr + "\n".join(f"{x['ts'].isoformat()}  {gpu:>3}  {x['temp']:>5}  {x['power']:>5}  {x['sm']:>5}  {x['sbe']:>3}  {x['dbe']:>3}  {x['remap']:>5}  {x['rfail']:>5}  {x['crc']:>7}  {x['replay']:>10}" for x in rows) + "\n"

if __name__ == "__main__":
    for n in SCENARIOS:
        for f in range(3):
            d = generate(n, 7, f); print(n, d["fmt"], len(d["files"]["dmesg.log"].splitlines()), "log lines")
