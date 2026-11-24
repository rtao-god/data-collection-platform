# Common commands

Docker runtime, the minimal service set, and the disposable integration database are owned by
`.codex/docker.md`. `verify`, `tests` and every lint/type/architecture command do not need Docker.

Read before choosing repository proof commands. Generic test-execution rules are owned by the shared
agent rules; this file holds project-specific entrypoints and exceptional commands only.

`tools/check.ps1` is the only command entrypoint. Every command runs from the repository root and
stops at the first failing step, returning that step's exit code.

# Verification

```powershell
pwsh -NoProfile -File D:\tools\utils\business-module-scanner\maintenance\Invoke-ProjectCommand.ps1 `
    D:\Aggregator\data-collection-platform pwsh -NoProfile -File tools/check.ps1 verify
```

`verify` is the default before broad work and before reporting a change complete. It runs, in order:
generated-contract drift, formatting, lint, strict typing, architecture boundaries, non-integration
tests, and first campaign snapshot validation.

# Scoped

```powershell
pwsh -NoProfile -File tools/check.ps1 contracts
pwsh -NoProfile -File tools/check.ps1 format
pwsh -NoProfile -File tools/check.ps1 lint
pwsh -NoProfile -File tools/check.ps1 types
pwsh -NoProfile -File tools/check.ps1 architecture
pwsh -NoProfile -File tools/check.ps1 tests
pwsh -NoProfile -File tools/check.ps1 campaign
```

Use a scoped command during a single-owner edit; `verify` is the task-stage gate, not a post-edit tax.

Scoped test runs select by path, node id, `-k`, or marker: `uv run pytest packages/collection_domain`
runs one owner's suite in well under a second. `tools/check.ps1 tests` is the whole non-integration
suite and only belongs at the task-stage gate.

`tests` excludes every integration marker. PostgreSQL, PostGIS, object storage, and container proof
need explicit infrastructure and are never part of `verify`.

# Backend submission

```powershell
uv run collector export --campaign-key <campaign> --run-id <run-id> `
    --candidate-revision <candidate-id>:<revision> --correlation-id <correlation-id>
uv run collector ingestion submit --export-path <sealed package path> --campaign-key <campaign> `
    --run-id <run-id> --candidate-revision <candidate-id>:<revision> `
    --target-site-key <site> --target-catalog-key <catalog> `
    --target-configuration-revision-id <active Catalog revision> `
    --producer-identity <producer> --producer-build <build> `
    --api-base-url <ingestion base url> --token-endpoint <token url> --client-id <client> `
    --correlation-id <correlation-id>
```

`submit` отправляет ровно подписанные байты экспорта и печатает `batchId`, `manifestDigest`
и replay-флаги. Workload-секрет и токены берутся только из окружения
(`COLLECTOR_INGESTION_CLIENT_SECRET`), никогда из аргументов и файлов. Дальнейшие шаги
(Ingestion → Catalog), replay и утверждения по сохранённым строкам Catalog:
`D:\Aggregator\aggregator-backend\docs\runbooks\ingestion-catalog-acceptance.md`.

# Registry → Catalog import

Активный путь публикации студий: реестр кампании → версионированный манифест → Catalog
registry-imports → редакционное одобрение → публикация.

```powershell
uv run python tools/registry_catalog_manifest.py --registry <studios.json> `
  --mapping campaigns/berlin_recording_services/catalog_mapping.yaml plan `
  --wfs-cache <dir> --out <manifest.json>
uv run python tools/registry_catalog_apply.py apply --manifest <manifest.json> `
  --base-url <catalog-api> --token-file <file> --checkpoint <file>
```

Контракт границы:

- `recordedAtUtc` — момент записи манифеста, и Catalog отвергает place revision, который
  старше несущего его evidence. Адресная точка наблюдается во время сборки манифеста, поэтому
  время записи вычисляется **после** записей: без `--recorded-at-utc` берётся не раньше самого
  позднего наблюдения, а явно заданное более раннее значение отвергается на месте (не поднимается
  молча). `recordedAtUtc` не входит в `manifestDigest`.
- `--wfs-cache` — каталог неизменяемых ответов адресного регистра: попадание в кэш даёт ту же
  точку и то же наблюдение, промах делает реальный запрос и записывает ответ.
- `429` от Catalog означает исчерпанный бюджет caller/operation-group и отказ **до** записи,
  поэтому тот же `Idempotency-Key` повторяется ограниченно (Retry-After либо 2/4/8/16/30 s, не
  более 6 попыток). Исчерпание повторов — явная ошибка с кодом и detail, а не пропуск записи.
- Повторный `apply` на неизменённом манифесте обязан давать `unchanged` по всем отправленным
  записям и не создавать новых `catalog.listing_revision`.

Экспорт читает **все** сохранённые строки кандидатов, поэтому приёмку запускают на
отдельной мигрированной базе: остатки интеграционных прогонов блокируют экспорт, а не
пропускаются.

## Approved rich fields → Catalog listing revision

Манифест несёт только name/address/categories/geography. Описания и контакты едут другим
путём — редакционной ревизией листинга, и только в состоянии, которое разрешил человек:

```powershell
uv run python tools/registry_catalog_enrich.py --registry <studios.json> `
  --base-url <catalog-api> --token-file <file> --recorded-at-utc <ts> `
  --out <plan.json> [--apply] [--only <registry-id> ...]
```

- Уезжает только `descriptions[locale].state == approved` с действующим решением ревью на
  точный `text_digest_sha256` и `source_evidence_digest_sha256`, и только контакт с
  `validation_state == validated`. `draft`, `pending_human` и `unvalidated` остаются в реестре:
  инструмент не повышает состояние и не записывает description review.
- Catalog публикует набор описаний целиком или никак, поэтому частичный набор локалей не
  отправляется (`description_locales_incomplete`).
- Контакт хранится в Catalog как абсолютный URI: `email` → `mailto:`, `phone` → `tel:`, `website`
  требует схему. Виды без записанной URI-формы (`whatsapp`, `booking_reference`,
  `map_reference`, `instagram`) отвергаются явно, а не подменяются другим видом.
- Слияние идёт поверх фактического содержимого ревизии (`GET listings/{id}/revisions/{revisionId}`),
  поэтому ручная правка в Catalog сохраняется. Отличающееся значение уже существующего поля —
  `conflict`, а не перезапись; повторный прогон на том же реестре даёт `unchanged` без записи.
- Никакого одобрения и публикации здесь нет: созданная ревизия ждёт `catalog.review`, а
  описания — отдельного approved-ревью на точный текст.
- `--only` принимает полный registry id: префикс выбирает 0 записей и печатает `records: 0`.
- Токену нужны три scope одновременно: `catalog.read-subjects` (binding), `catalog.import-registry`,
  `catalog.edit-listing` (чтение ревизии и запись новой).
- `binding.contentDigest` — это digest исходной записи, который сохранил
  `CatalogRegistryImportService`; это не digest ревизии Catalog, и они не могут совпасть. Проверка
  привязки — идентичность листинга (`binding.listingId == listing.id`), а не равенство digest.
- Часть сайтов отдаёт разные байты на каждый запрос (`hansastudios.de`, `studio1058.de`). Описание
  привязывается к digest того артефакта, который записан в том же прогоне capture, иначе ревизия
  будет ссылаться на digest, которого нет в реестре.

Проверенный сквозной путь (TRIXX, 2026-11-22): ревизия 7 создана с `expectedVersion 28`, повторный
прогон дал `unchanged` без новой ревизии; `catalog.review` требует `POST
listings/{id}/description-reviews` по каждой локали **до** approval, иначе approval отвечает 409
`has no current approved human review of the exact reviewed bytes`. Ревьюер в Catalog не может быть
тем же actor, что создал ревизию (400 `The reviewer must be a different actor from the revision
generator`) — токен ревьюера минтится с отдельным `actor_id`.

Публичная выдача локализуется по **именам** листинга, а не по описаниям: локаль без
`names[locale]` отдаёт карточку в fallback на default locale, даже если описание в этой локали
утверждено и лежит в ревизии. У записей реестра есть только `canonical_name`, поэтому en-GB/ru-RU
описания доходят до ревизии, но публичная карточка для них остаётся de-DE (`translationState:
fallback`). Чтобы три локали реально отдавались, нужны локализованные имена листинга — это отдельное
переводческое решение, а не следствие одобрения описания.

## Fresh official-site evidence и человеческие решения в реестре

Три команды владельца реестра, каждая пишет только своё и ничего не одобряет сама.

```powershell
# 1. Снять реальные страницы официального сайта (0.5 rps, robots проверяется, редирект не
#    выполняется, отказ 401/403/429 останавливает запись). Пишется capture-манифест и сырые байты.
uv run python tools/registry_evidence_capture.py --requests <requests.json> `
  --cache <dir> --out <capture.json>
```

- Формат запроса: список объектов `{registryId, url, fieldPaths, mustContain}`. `mustContain` —
  фразы, которые страница обязана содержать дословно. Любая отсутствующая фраза — отказ
  `fact_not_stated`, а не укороченная цитата.
- `excerpt` ограничен 400 символами: при длинном совпадении цитата сужается вокруг первой фразы,
  но всегда сохраняет её дословно. Поле `factLiterals` не читается — литералы передаются в
  `mustContain`.

```powershell
# 2. Приложить снятые артефакты к записям (одна запись = один артефакт на объявленный field path).
#    Повторный прогон не дублирует: одинаковые url+digest+field_path не пишутся дважды.
uv run python tools/studio_registry.py record-evidence --capture <capture.json> [--dry-run]

# 3. Решение владельца о политике источника и о медиа.
uv run python tools/studio_registry.py decide --policy-digest <уже одобренный digest> `
  --reviewer <id> --decided-at-utc <ts>
uv run python tools/studio_registry.py decide --omit-media "<причина>" `
  --reviewer <id> --decided-at-utc <ts>
```

- `--policy-digest` принимает только digest, который в этом реестре уже зафиксирован как
  одобренная политика: новая запись продолжает существующую идентичность политики, а не
  изобретает её. Переданный вместе с `--policy-file` — отказ.
- В реестре сосуществуют три записанных digest политики: `78ab20c8…`, `9a9f7979…` и
  `221e668e…` (последний — sha256 текущего файла `official_site_berlin_studios_policy.yaml`).
  Текущая версия политики уже одобрена здесь, поэтому новая съёмка не требует нового одобрения
  политики, но каждый артефакт всё равно требует собственного решения: одобрение,
  записанное для другого артефакта или для другого digest, не переносится.
- `prohibited_fields: description_text` запрещает брать текст страницы как поле источника, а
  комментарий политики — копировать или перефразировать его в описание. Поэтому редакционное
  описание допустимо только как собственный текст, факты которого страница подтверждает
  дословно; проверка оригинальности — самый длинный общий отрезок слов с этой страницей.
- `enrich` пропускает описание/контакт только если артефакт его источника проходит
  `evidence_allows_publication`, то есть несёт одобренное решение политики. Артефакт без
  решения даёт `description_evidence_not_publishable:<locale>` /
  `contact_evidence_not_publishable:<kind>`.
- `--omit-media` записывает `photo_candidates` с `rights_state=omitted_rights_unclear` и
  причиной: это закрывает `media_state_not_recorded` без выдуманной лицензии. Уже имеющиеся
  кандидаты не перезаписываются (`media_refused`).

```powershell
# 4. Решение человека по черновику и контакту. Единственный путь к state=approved/validated.
uv run python tools/studio_registry.py review --reviewer <id> --decided-at-utc <ts> `
  --descriptions <decisions.json> [--contacts <decisions.json>] [--dry-run]
```

- `reviewer`, начинающийся с `research-`, отвергается: инструмент, который составил черновик или
  снял страницу, не ревьюит сам себя.
- Решение по описанию несёт `text_digest_sha256`; если текст изменился после чтения — отказ
  (`text changed since the reviewer read it`), старое одобрение не наследуется.
- Решение по контакту несёт `kind`+`target`; digest источника берётся из evidence этого контакта.
  Указанный в решении digest обязан совпадать с digest одного из артефактов, на которые
  ссылается сам контакт, иначе решение отклоняется: чужой digest не может стать provenance.
- Любой отказ в файле решений отклоняет всю пачку и ничего не записывает (exit code 1):
  недопустимо оставлять полу-применённое ревью.
- Записи без решения остаются черновиками: `rich` в `report` остаётся 0, пока ревью не записано.

# Campaign authoring

```powershell
uv run collector config validate berlin_recording_services
uv run collector config digest berlin_recording_services
```

# Integration and infrastructure

These need a running PostgreSQL/PostGIS or SeaweedFS and are never run by `verify`:

```powershell
COLLECTOR_DATABASE_URL=<runtime secret> uv run pytest -m integration database/tests
COLLECTOR_DATABASE_URL=<runtime secret> uv run collection-migrate upgrade head
```

Migration is an explicit state-changing operation. No API, worker, readiness check, or image startup
runs it.

Integration tests are repeatable against one migrated database: every fixture derives per-run unique
identities, so rerunning the suite must not need a fresh database. CI runs the database contract twice
against the same instance to keep that property enforced.

The disposable integration database is started, migrated and removed by its own owner script:
`pwsh -NoProfile -File tools/integration-db.ps1 up|test|down`. It keeps a separate Compose project,
volume-less tmpfs data and its own migration run. `up` treats the database as ready only when the
application database answers a TCP query: the image's init phase keeps a temporary server on the unix
socket only, and a socket-only readiness query used to pass while that server was already shutting
down, so migrations failed with an `OperationalError` instead of retrying.

# Runtime

Worker Gateway needs exact object-store configuration and mounted secret files:
`WORKER_GATEWAY_TOKEN_FILE`, `COLLECTOR_OBJECT_STORE_ENDPOINT`,
`COLLECTOR_OBJECT_STORE_BUCKET`, `COLLECTOR_OBJECT_STORE_ACCESS_KEY_FILE`,
`COLLECTOR_OBJECT_STORE_SECRET_KEY_FILE`, `COLLECTOR_OBJECT_STORE_REGION`.

Compose and image commands live in `deploy/compose/` and `deploy/docker/`.
