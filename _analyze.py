import json, re

d = json.load(open("tracker.json"))
rows = d["rows"]

def num(s):
    if s is None: return None
    s = str(s).replace(",", "").strip()           # 15,000 -> 15000
    found = re.findall(r"\d{3,}(?:\.\d+)?", s)     # only real prices (>=3 digits)
    if not found: return None
    vals = [float(x) for x in found]
    return sum(vals)/len(vals)                      # range midpoint

def conf(s):
    if not s: return None
    m = re.search(r"(\d+)", str(s))
    return int(m.group(1)) if m else None

analyzed = []
for r in rows:
    ai = num(r["ai_price"])
    new = num(r["new_price"])
    internal = num(r["internal_price"]) if r["internal_price"] and "n/a" not in r["internal_price"].lower() else None
    cust = num(r["customer_price"]) if r["customer_price"] and not re.search(r"[a-z]{4}", r["customer_price"].lower()) else None
    c = conf(r["confidence"])
    flags = []
    if ai and new and ai > new:
        flags.append("AI>NEW(impossible)")
    if ai and new and ai > 0.85*new:
        flags.append("AI>85%%new")
    if ai and internal:
        ratio = ai/internal
        if ratio >= 1.4: flags.append("AI>=1.4x internal (%.2f)" % ratio)
        elif ratio <= 0.6: flags.append("AI<=0.6x internal (%.2f)" % ratio)
    analyzed.append((r, ai, new, internal, cust, c, flags))

# Categorize: GREEN = AI within sane band of internal (0.6-1.4x) OR (no internal) AI in 10-60% of new
def verdict(ai, new, internal):
    if ai is None: return "no-ai"
    if ai and new and ai > new: return "RED: AI>NEW"
    if internal and internal > 0:
        rr = ai/internal
        if 0.6 <= rr <= 1.4: return "GREEN"
        return "RED: AI=%.2fx internal" % rr
    if new and new > 0:
        rr = ai/new
        if 0.10 <= rr <= 0.60: return "green? (%.0f%% of new, no internal)" % (rr*100)
        if rr > 0.60: return "RED: AI=%.0f%% of new (too high)" % (rr*100)
        return "RED: AI=%.0f%% of new (too low)" % (rr*100)
    return "no-benchmark"

recent = [a for a in analyzed if a[5] is not None]
buckets = {}
for r, ai, new, internal, cust, c, flags in recent:
    v = verdict(ai, new, internal)
    key = v.split(":")[0].split("(")[0].strip()
    buckets[key] = buckets.get(key, 0) + 1

print("=== VERDICT SUMMARY (rows with confidence): %d ===" % len(recent))
for k, n in sorted(buckets.items(), key=lambda x: -x[1]):
    print("  %-28s %d" % (k, n))

print()
print("=== RED CASES vs INTERNAL (human benchmark exists) ===")
for r, ai, new, internal, cust, c, flags in recent:
    v = verdict(ai, new, internal)
    if v.startswith("RED") and internal:
        print("%-34s | ai=%-8s int=%-8s new=%-10s conf=%s | %s" % (
            r["item"][:34], r["ai_price"], r["internal_price"], r["new_price"], c, v))

print()
print("=== RED: AI > NEW (impossible) ===")
for r, ai, new, internal, cust, c, flags in recent:
    if ai and new and ai > new:
        print("%-34s | ai=%-8s new=%-10s int=%-8s conf=%s" % (
            r["item"][:34], r["ai_price"], r["new_price"], r["internal_price"], c))
