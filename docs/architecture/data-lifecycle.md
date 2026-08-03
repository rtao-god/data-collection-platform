# Data lifecycle

The final lifecycle is:

```text
ConfigBundle
-> CollectionRun
-> StageRun
-> WorkUnit
-> FetchObservation
-> RawArtifact
-> ExtractedRecord
-> FieldObservation
-> CandidateEntityRevision
-> GeographyEvaluation
-> MatchProposal / EntityClusterRevision
-> QualityEvaluation
-> ReviewCase
-> ReviewDecision
-> CuratedCandidateRevision
-> ExportBatch
```

The current implementation proves only the first owner transition:

```text
versioned campaign files
-> allowlisted raw bundle
-> strict typed documents
-> cross-document validation
-> canonical representation
-> deterministic SHA-256 snapshot identity
```

No later lifecycle object is fabricated to make the repository look complete.
