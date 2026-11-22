"""Worker packer: LPT for every k in 1..8, keep k minimising max_load + 2000ms."""
WORKER_MS = 2000
MAX_WORKERS = 8


def pack(items):
    """items: list of (test_number, duration_ms). Returns list of workers (lists of numbers)."""
    if not items:
        return []
    items = sorted(items, key=lambda x: -x[1])
    best = None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n, d in items:
            i = loads.index(min(loads))
            ws[i].append(n)
            loads[i] += d
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return [w for w in best[1] if w]
