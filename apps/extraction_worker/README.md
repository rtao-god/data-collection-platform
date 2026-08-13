# Extraction worker

`extraction_worker` processes the non-source Work Engine capability `extraction`.

The `sourceKey` inside `extraction-request@1` is provenance inherited from acquisition evidence. It is not a source-capacity permit. The worker rejects any extraction lease carrying `source_key`, verifies the exact raw artifact identity and digest declared by the request, performs bounded inert extraction through `extraction_core`, uploads one canonical typed-observation bundle through Worker Gateway, and completes only the exact active lease.

The image has no crawler, browser, database, migration, or direct Object Store implementation ownership. Crash recovery and retry remain entirely in Work Engine.
