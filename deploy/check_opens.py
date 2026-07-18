import boto3
from collections import Counter

ddb = boto3.resource("dynamodb", region_name="us-east-1")
table = ddb.Table("ep-v3-prod")

# All WEBHOOK raw rows
resp = table.scan(
    FilterExpression="begins_with(pk, :p)",
    ExpressionAttributeValues={":p": "WEBHOOK#"},
    Limit=100,
)
items = resp["Items"]
print(f"WEBHOOK rows: {len(items)}")
counts = Counter(it.get("event_type") for it in items)
for t, n in counts.most_common():
    print(f"  {t}: {n}")

# EVENT rows (event_log synced to terminal)
resp2 = table.scan(
    FilterExpression="pk = :p",
    ExpressionAttributeValues={":p": "EVENT"},
    Limit=100,
)
events = resp2["Items"]
print(f"\nEVENT rows (synced): {len(events)}")
ecounts = Counter(it.get("table_name") for it in events)
for t, n in ecounts.most_common():
    print(f"  {t}: {n}")

# Check terminal dispatched_job outcomes
print("\nChecking terminal DB for opened outcomes...")
