import csv, collections, re
from app import parse_duration

for fn in ['Report_7602_23-10-2026-11-21-46.csv','Report_7602_23-10-2026-01-10-13.csv','Report_7887_23-10-2026-11-46-18.csv']:
    print('===', fn)
    rows=list(csv.DictReader(open(fn, encoding='utf-8-sig')))
    rec=[r for r in rows if (r.get('Recording URL') or '').strip()]
    gt5rec=[r for r in rec if parse_duration(r.get('Total Duration'))>5]
    print('rows',len(rows),'rec',len(rec),'gt5rec',len(gt5rec))
    hosts=collections.Counter(re.sub(r'^https?://([^/]+).*',r'\1',(r.get('Recording URL') or '').strip()) for r in rec)
    print(hosts.most_common())
    if rec: 
        print(rec[0].get('Recording URL'))
    print()

