# Pipeline advancement

Цель: превратить один успешный выход работы в одно явное следующее действие, сохранив это решение долговечно.

`pipeline-advancement/advancement` — planning and applying deterministic transitions from successful work, Worker Gateway runtime composition, durable advancement state, and pure candidate-materialization adapter from verified observation batches to immutable candidate revisions and resolution inputs.

Goal: verified `field-observation-batch@1` + exact source artifact identities + explicit field projection → revision-0 `CandidateRevision` + `ResolutionCandidate`. Recompute observation identities and batch digest before projection; preserve every observation payload and derive field evidence from those observations. Accept only validated `UNKNOWN` geography until a producer proves observation-to-coordinate and exact market-area provenance. The adapter is pure and does not write DB, artifacts, or work state.

Цепочки:

Verified field-observation batch + exact source artifact identities + explicit field projection → candidate revision + resolution input with `UNKNOWN` geography only.

Успешная работа → точный выходной артефакт → план перехода → применение: постановка downstream-работы и applied-состояние в одной транзакции.

`ManualRecordLineage@1` contract identifies exact manual-record work/artifact and expected entity kind without changing `manual-import-record@1` bytes.

Принятая seed-запись → детерминированная robots-работа → typed-решение для точного seed URL → page-работа только при разрешении и с точной robots evidence identity; при запрете — явный blocked outcome без page-работы.

Запланированный или ручной Dagster run → аутентифицированный внутренний endpoint Worker Gateway → существующие владельцы Pipeline Advancement → durable результат.

Неизвестный переход → блокировка с контекстом владельца, никогда не пропуск.

Границы:

- Collection DB — единственный atomic checkpoint; Dagster вызывает advancement через authenticated Worker Gateway и хранит только orchestration history в отдельной metadata DB;
- у каждой успешной работы есть строка advancement либо терминальное исключение, определённое владельцем; каждый терминальный блокер виден в coverage запуска;
- применение требует точного источника выхода, плана перехода, ID и токена аренды, идентичности исполнения Dagster и непросроченной аренды;
- worker'ы остаются без учётных данных БД;
- план manual import декодируется только каноническим контрактом; допускающий к алиасам парсер удалён;
- одна принятая manual-запись становится одной детерминированной `manual_record` работой.

Tests: `database/tests`, `packages/collection_application/tests`, `packages/collection_infrastructure/tests`, `packages/candidate_materialization/tests`.

Философия: неизвестное состояние блокирует, а не угадывается. Переход либо применён целиком, либо явно заблокирован с ответственным владельцем.
