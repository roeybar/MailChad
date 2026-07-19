import boto3, base64

ddb = boto3.resource("dynamodb", region_name="us-east-1")
table = ddb.Table("ep-v3-prod")

# Check pubkeys directly
for actor in ("operator", "client"):
    resp = table.get_item(Key={"pk": f"PUBKEY#{actor}", "sk": f"PUBKEY#{actor}"})
    item = resp.get("Item")
    if item:
        pub = item.get("kem_pub")
        print(f"{actor}: kem_pub={base64.b64encode(pub.value if hasattr(pub,'value') else bytes(pub)).decode()[:20]}... registered={item.get('registered_at')}")
    else:
        print(f"{actor}: NOT FOUND")

# Simulate load_pubkeys to see if it would fail
print("\nSimulating load_pubkeys...")
try:
    import nacl.public
    out = {}
    for actor in ("operator", "client"):
        resp = table.get_item(Key={"pk": f"PUBKEY#{actor}", "sk": f"PUBKEY#{actor}"})
        item = resp.get("Item")
        if item:
            raw = item.get("kem_pub")
            # Handle both bytes and Binary types
            if hasattr(raw, 'value'):
                raw = raw.value
            else:
                raw = bytes(raw)
            out[actor] = nacl.public.PublicKey(raw)
            print(f"  {actor}: OK ({len(raw)} bytes)")
    if "operator" not in out or "client" not in out:
        print(f"  MISSING: got {list(out.keys())}")
    else:
        print("  Both pubkeys loaded successfully!")
except Exception as e:
    print(f"  ERROR: {e}")
