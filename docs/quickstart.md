# Five-minute path

```bash
export CROSSING_ALLOW_DEV=1
pip install -r requirements.txt
PYTHONPATH=. python -m pytest tests -q
uvicorn crossing.api:app --reload
```

In another shell:

```python
from crossing.sdk import Crossing, CrossingClient

# in-process
cx = Crossing.in_process()
p = cx.create_principal("Alice")
a = cx.create_agent(p.id, "researcher")
m = cx.issue_mandate(p.id, a.id, 100, tools=["search"], servers=["mock"])
r = cx.invoke(m.id, "search", {"q": "hi"}, idempotency_key="k1")
assert r.ok and cx.verify_receipt(r.receipt)
assert r.receipt["body"]["kid"]
print("remaining", cx.remaining_budget(m.id))
print("account", cx.account(p.id))
print("billing", cx.billing_status(p.account_id))

# HTTP /v1 (dev bootstrap key secret is the word dev)
http = CrossingClient("http://127.0.0.1:8000", "dev")
http.authenticate()
```

Auth header is `X-API-Key`. Production needs `CROSSING_ED25519_SEED` and `CROSSING_KEY_PEPPER`.
