#!/bin/bash
# Test upload of a small FBX mesh to Roblox Open Cloud
# This reproduces the timeout issue — the POST succeeds but the async operation
# never completes within the poll window.

API_KEY=$(cat /Users/nicktornow/unity/apikey)
CREATOR_ID=$(cat /Users/nicktornow/unity/creator_id)
FILE="/Users/nicktornow/unity/test_projects/SimpleFPS/Assets/AssetPack/Pallet/pallet03_prp.fbx"

echo "=== Step 1: Upload ==="
RESPONSE=$(curl -s -X POST "https://apis.roblox.com/assets/v1/assets" \
  -H "x-api-key: $API_KEY" \
  -F "request={\"assetType\":\"Model\",\"displayName\":\"pallet03_prp\",\"description\":\"\",\"creationContext\":{\"creator\":{\"userId\":\"$CREATOR_ID\"}}};type=application/json" \
  -F "fileContent=@$FILE;type=model/fbx")

echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"

# Extract operation ID
OP_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('operationId') or d.get('path','').split('/')[-1])" 2>/dev/null)
echo ""
echo "Operation ID: $OP_ID"

if [ -z "$OP_ID" ]; then
  echo "No operation ID — upload may have returned asset ID directly"
  exit 0
fi

echo ""
echo "=== Step 2: Poll operation ==="
for i in $(seq 1 30); do
  sleep 2
  POLL=$(curl -s "https://apis.roblox.com/assets/v1/operations/$OP_ID" \
    -H "x-api-key: $API_KEY")
  DONE=$(echo "$POLL" | python3 -c "import sys,json; print(json.load(sys.stdin).get('done', False))" 2>/dev/null)
  echo "Poll $i: done=$DONE"
  if [ "$DONE" = "True" ]; then
    echo "$POLL" | python3 -m json.tool
    break
  fi
done
