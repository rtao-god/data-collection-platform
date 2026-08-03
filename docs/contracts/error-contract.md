# Error contract

Every expected failure crossing an application or transport boundary uses the canonical envelope:

```json
{
  "type": "collection/campaign-contract-invalid",
  "owner": "CampaignConfiguration",
  "code": "CAMPAIGN_CONTRACT_INVALID",
  "message": "Campaign configuration does not satisfy its owner contract.",
  "context": {},
  "requiredAction": "Correct the named campaign document and validate it again.",
  "correlationId": "..."
}
```

The envelope must name the owner, expected/factual context through typed code and structured
context, required action, and correlation ID. Expected contract failures must not escape as an
uncontextualized exception or a successful empty payload.
