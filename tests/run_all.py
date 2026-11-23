"""Run every sample through the pipeline (fresh state) and assert the expected decision."""
import json, os, sys, tempfile
os.environ.setdefault("DEMO_PACING_MS", "0")
os.environ["RUNTIME_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import store
from app.pipeline import Pipeline
store.init()
manifest = json.load(open(store.SAMPLES / "manifest.json"))
fails = 0
def run(fname):
    data = (store.SAMPLES / fname).read_bytes()
    import hashlib
    rid = store.create_run(fname, hashlib.sha256(data).hexdigest())
    res = Pipeline(rid, data, fname, lambda e: None).run()
    store.finish_run(rid, res)
    return res
for m in manifest:
    res = run(m["file"])
    d = res["decision"]
    ok = d["outcome"] == m["expected"]
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {m['file']:<48} {d['outcome']:<9} ({res['engine']}, {res['duration_ms']} ms)")
    print(f"      {d['headline']}")
    if "-v" in sys.argv:
        for st in res["stages"]:
            for c in st["checks"]:
                print(f"        [{st['key']:<9}] {c['status']:<6} {c['label']}: {c['detail']}")
        print("      NEXT:", d["next_action"])
        if d["vendor_message"]: print("      MSG:", json.dumps(d["vendor_message"])[:300])
# re-running the approved happy path must now be caught as a duplicate
res = run(manifest[0]["file"])
ok = res["decision"]["outcome"] == "REJECTED"; fails += not ok
print(f"{'PASS' if ok else 'FAIL'}  re-run of happy path -> {res['decision']['outcome']}: {res['decision']['headline']}")
print("FAILURES:", fails); sys.exit(1 if fails else 0)
