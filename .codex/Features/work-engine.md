# Work engine

Цель: довести запрос до управляемого запуска, выполнить семантические работы под арендой и сохранить неизменяемые артефакты как доказательство.

`work-engine/run-control` — создание запусков и стадий, управление запуском оператором, coverage и операторская HTTP-граница.

`work-engine/work-execution` — семантика work unit, lease и heartbeat, классификация retry, ёмкость источника, протокол Worker Gateway и его Collection-owned runtime composition.

`work-engine/artifact-lifecycle` — загрузка, проверка, привязка ролей, content-addressed хранение, очистка и обработка сирот.

Цепочки:

Точный опубликованный снапшот `ready` → создание запуска и стадий → начальная работа в одной транзакции.

Worker → регистрация → аренда (`FOR UPDATE ... SKIP LOCKED`) → heartbeat → загрузка и проверка артефактов → завершение → освобождение ёмкости.

Оператор → точная ожидаемая ревизия → pause/resume/cancel → append-only история переходов.

Попытка → классифицированный сбой → ограниченный retry → dead letter.

Границы:

- Collection DB — единственный владелец atomic checkpoint; пауза блокирует новые аренды через каноническое состояние запуска, без второго флага планировщика;
- Dagster вызывает Pipeline Advancement через закрытый authenticated Worker Gateway endpoint; Gateway собирает существующих Collection owners, Dagster не получает Collection DB credentials;
- завершение требует точных ID работы, аренды, токена, worker'а, build identity, входного digest и привязок выходных артефактов;
- `attempt_count` считает каждую аренду, retry-budget расходует только `failure_count`, поэтому безопасное освобождение не становится сбоем;
- физический контент переиспользуется по kind и digest, но каждая завершённая работа создаёт отдельную неизменяемую запись артефакта;
- worker'ы не получают учётные данные PostgreSQL или объектного хранилища и не ходят в БД напрямую;
- оператор аутентифицирован; тело запроса не выбирает actor'а;
- состояние запуска — каноническая операторская истина в `runs.collection_runs`, переходы — команды точной конкурентности в append-only `runs.collection_run_transitions`;
- пауза обеспечивается запросом аренды Work Engine; cancel обновляет только незанятые pending/retry работы, сохраняя неизменяемые попытки и артефакты;
- API отдаёт только liveness/readiness, create/read/coverage/pause/resume/cancel и маршруты ревью; startup-миграция запрещена, документационные маршруты отключены;
- coverage сообщает точные счётчики состояний работ плюс явные блокеры запуска, стадии, dead letter и политики;
- readiness проверяет нужные таблицы PostgreSQL и зависимость объектного хранилища, не изменяя ни одну из них;
- миграции идут только через выделенный миграционный процесс; worker, API, readiness и старт образа миграций не выполняют;
- внешние ключи не каскадно удаляют доказательства; PostGIS остаётся инфраструктурной предпосылкой при downgrade.

Философия: работа берётся только из канонической очереди; аренда, попытка и артефакт — разные смыслы; терминальный сбой оставляет dead letter, не удаляя прежние попытки.

Физические схемы (Alembic и SQLAlchemy metadata):

- `runs.collection_runs`, `runs.stage_runs`, append-only `runs.collection_run_transitions`;
- `sources.source_capacity_states`, `sources.artifact_uploads`, `sources.artifact_objects`, `sources.artifact_records`;
- `work.worker_registrations`, `work.worker_capabilities`, `work.worker_output_contracts`, `work.worker_heartbeats`;
- `work.work_units`, `work.work_attempts`, `work.dead_letters`;
- `work.work_input_artifacts`, `work.work_output_artifacts`.

Tests: `packages/collection_domain/tests`, `packages/collection_application/tests`, `packages/collection_infrastructure/tests`, `packages/source_connector_sdk/tests`, `database/tests`, `apps/control_api/tests`, `apps/worker_gateway/tests`, `apps/collector_cli/tests`, `tools/control_api_contract_generation/tests`.
