import csv
from io import StringIO

def parse(text: str):
    lines = [line.strip() for line in text.strip().splitlines() if line.strip() and not line.startswith('#')]
    if not lines:
        return []
    # Assume first line is header
    header = lines[0]
    # Use csv reader to handle quoted fields etc.
    f = StringIO('\n'.join(lines))
    reader = csv.DictReader(f)
    result = []
    for row in reader:
        # Convert timestamp
        ts = row.get('timestamp') or row.get('Timestamp') or ''
        # GPU index
        gpu_str = row.get('gpu') or row.get('GPU') or row.get('gpu_id') or ''
        try:
            gpu = int(gpu_str)
        except:
            gpu = None
        # Helper to get float/int
        def get_float(key, default=0.0):
            try:
                return float(row.get(key, default))
            except:
                return default
        def get_int(key, default=0):
            try:
                return int(float(row.get(key, default)))  # handle floats like 0.0
            except:
                return default
        # Map columns
        temp_c = get_float('temp_c')
        power_w = get_float('power_w')
        sm_clock_mhz = get_float('sm_clock_mhz')
        # Counters: may have _total suffix
        ecc_sbe = get_int('ecc_sbe_total') if 'ecc_sbe_total' in row else get_int('ecc_sbe')
        ecc_dbe = get_int('ecc_dbe_total') if 'ecc_dbe_total' in row else get_int('ecc_dbe')
        remapped_rows = get_int('remapped_rows')
        remap_failures = get_int('remap_failures')
        nvlink_crc = get_int('nvlink5_crc_errors') if 'nvlink5_crc_errors' in row else get_int('nvlink_crc')
        pcie_replay = get_int('pcie_replay_count') if 'pcie_replay_count' in row else get_int('pcie_replay')
        result.append({
            'ts': ts,
            'gpu': gpu,
            'temp_c': temp_c,
            'power_w': power_w,
            'sm_clock_mhz': sm_clock_mhz,
            'ecc_sbe': ecc_sbe,
            'ecc_dbe': ecc_dbe,
            'remapped_rows': remapped_rows,
            'remap_failures': remap_failures,
            'nvlink_crc': nvlink_crc,
            'pcie_replay': pcie_replay
        })
    return result