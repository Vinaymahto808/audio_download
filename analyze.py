import csv, collections, re
rows=list(csv.DictReader(open("Report_7887_22-10-2026-02-53-52.csv", encoding="utf-8-sig")))
print("rows:", len(rows))
print("cols:", len(rows[0]))
def secs(s):
    s=(s or "").strip()
    if not s: return None
    p=s.split(":")
    try:
        p=[int(x) for x in p]
    except: return None
    while len(p)<3: p.insert(0,0)
    return p[0]*3600+p[1]*60+p[2]
dur=[secs(r["Total Duration"]) for r in rows]
print("dur parse fail:", sum(1 for d in dur if d is None))
print("max dur:", max(d for d in dur if d is not None), "min:", min(d for d in dur if d is not None))
rec=[r for r in rows if (r["Recording URL"] or "").strip()]
print("with recording url:", len(rec))
print("without:", len(rows)-len(rec))
gt5=[r for r,d in zip(rows,dur) if (d or 0)>5]
print("dur>5s:", len(gt5))
gt5rec=[r for r in rec if (secs(r["Total Duration"]) or 0)>5]
print("rec AND dur>5:", len(gt5rec))
# recording present but dur<=5
low=[r for r in rec if (secs(r["Total Duration"]) or 0)<=5]
print("rec but dur<=5:", len(low))
print("dial status:", collections.Counter(r["Dial Status"] for r in rows).most_common(10))
print("sample urls:", [r["Recording URL"] for r in rec[:3]])
hosts=collections.Counter(re.sub(r"^https?://([^/]+).*",r"\1",r["Recording URL"].strip()) for r in rec)
print("hosts:", hosts.most_common())
