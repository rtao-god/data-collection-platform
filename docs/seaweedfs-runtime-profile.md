# SeaweedFS runtime profile

The repository contains a digest-pinned local S3-compatible profile:

```bash
docker compose -f deploy/compose/seaweedfs.yml up --build
```

The profile starts SeaweedFS with its S3 endpoint bound only to `127.0.0.1`, then runs the repository-owned bucket bootstrap command. Local credentials are development-only defaults and must not be reused outside the local profile.

The permanent CI workflow runs an independent live compatibility job against the same pinned image. The test proves the object lifecycle required by the platform:

1. pre-signed `PUT` to a staging key;
2. streamed read and SHA-256 verification;
3. server-side promotion to a content-addressed key;
4. pre-signed `GET` of the promoted object;
5. idempotent deletion through the cleanup adapter;
6. absence of the staging object while the promoted object remains readable.

The profile does not run database migrations implicitly and does not grant connector workers PostgreSQL credentials.
