# Manual import

Цель: превратить авторские seed-файлы оператора в проверенные неизменяемые записи и поставить их как семантическую работу.

`manual-import/admission` — разбор CSV/JSON/JSONL, точные локаторы, валидация строк, digest'ы плана и записи, допуск плана, work unit на каждую запись и хосты manual import worker.

Цепочки:

Seed-файл → UTF-8 → план + локаторы → валидация строк → issues или записи → digest → допуск по политике источника → одна детерминированная `manual_record` работа на schedulable-запись.

План → каноническое декодирование → неизменяемый `manual-import-record@1` артефакт.

Границы:

- atomic mode не планирует ни одной записи при любом issue; partial принимается только по явному запросу и разрешению политики источника;
- номера строк CSV и JSONL — физические локаторы источника, не индексы;
- `manual_record` не требует source permit: обрабатывается уже допущенный неизменяемый артефакт;
- принятая запись не становится нормализованным наблюдением или кандидатом автоматически;
- нижележащий сбор остаётся заблокированным, пока одобренный владелец источника не сопоставит запись с точной работой.

Философия: план переиспользуем только после проверки его канонической смысловой и артефактной идентичности. Proof-строка не является записью.

Артефакт записи сохраняет роль источника, digest источника, digest плана, digest артефакта плана, выбранную позицию, локатор и digest записи. `ManualRecordLineage@1` отдельно связывает run, точную manual-record работу и artifact, semantic/content digests записи, expected entity kind и исходный artifact. Каждый процесс регистрирует ровно одну capability: `manual_import` или `manual_record`.

Tests: `packages/manual_import_core/tests`, `packages/collection_contracts/tests`, `packages/collection_application/tests`, `packages/collection_infrastructure/tests`, `database/tests`, `apps/manual_import_worker/tests`.
