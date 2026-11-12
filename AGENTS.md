# Data Collection Platform agent routing

# Project scope

This repository owns collection-source policy, campaign snapshots, durable collection work,
raw evidence, typed observations, candidate resolution, quality/review state, and sealed
collector exports. It does not own public catalog listings, SEO, billing, booking, publication,
or future Aggregator Platform database.

# Required shared rules
Always read `D:\tools\Agents_Rules\Backend.md`.

# Project identity

- Python 3.13 and one `uv` workspace own backend and pipeline dependencies.
- Campaign configuration is declarative data, never executable code.
- Workers must not receive Collection DB credentials; Worker Gateway is future runtime boundary.
- Database migrations run only through dedicated migration composition root.
- current implementation must fail explicitly on incomplete or invalid owner state.
- A collector-owned sealed export is repository boundary; no temporary Aggregator DTO is allowed.

# Project-specific paths

- Campaign authoring: `campaigns/`
- Python deployables: `apps/`
- Python owner packages: `packages/`
- Database schema and migration history: `database/`
- Generated machine-readable contracts: `contracts/`
- Architecture and operator documentation: `docs/`
- Repository checks and generators: `tools/`
- Temporary local artifacts: `.tmp/` only; never commit them.

# Тесты

Тесты лежат рядом с владельцем, в `<package-or-app>/tests/`: один пакет — один каталог тестов. Отдельного `Tests/` нет, бизнес-модуль не является каталогом — модуль покрывает несколько пакетов, поэтому его тестовые каталоги перечислены в описании модуля (`.codex/Features/<module>.md`).

Запуск: `uv run pytest <path>` гоняет тесты одного владельца. Маркеры `integration` и `object_store_integration` требуют явной инфраструктуры и не входят в `verify`.

# Бизнес модули

Модуль Campaign configuration

Превращает авторские YAML в проверенную конфигурацию кампании и неизменяемый снапшот с каноническими digest'ами. Runtime читает только снапшот, а не изменяемые файлы; заблокированная готовность остаётся заблокированной.

`.codex/Features/campaign-config.md`

Модуль Work engine

Доводит запрос до управляемого запуска, выполняет семантические работы под арендой и сохраняет неизменяемые артефакты как доказательство. Точно разделяет аренду, попытку и артефакт; worker'ы не получают доступа к базе или объектному хранилищу.

`.codex/Features/work-engine.md`

Модуль Manual import

Превращает авторские seed-файлы оператора в проверенные неизменяемые записи и ставит их как семантическую работу. Atomic mode не планирует ничего при любом issue.

`.codex/Features/manual-import.md`

Модуль OSM acquisition

Получает данные OpenStreetMap по настроенной рыночной области с сохранённым provenance.

`.codex/Features/osm-acquisition.md`

Модуль HTTP acquisition

Получает содержимое официальных сайтов с соблюдением политики и оставляет неизменяемое сырое доказательство. Каждый ответ — либо доказательство, либо типизированный отказ.

`.codex/Features/http-acquisition.md`

Модуль Evidence processing

Извлекает из сырого доказательства типизированные наблюдения с явными состояниями. Отсутствие, запрет и недопустимое значение не подменяются пустым успехом.

`.codex/Features/evidence-processing.md`

Модуль Entity resolution

Детерминированно сопоставляет кандидатов, строит обратимые кластеры и выносит fail-closed вердикт качества. Сомнение трактуется как ревью, не как совпадение.

`.codex/Features/entity-resolution.md`

Модуль Candidate review

Принимает и сохраняет редакционные решения, ручные исправления и подавления. Решения append-only: повторное решение создаёт новую ревизию, а не изменяет прежнюю.

`.codex/Features/candidate-review.md`

Модуль Pipeline advancement

Превращает один успешный выход работы в одно явное следующее действие, сохраняя это решение долговечно. Неизвестный переход блокирует с контекстом владельца, не пропускается молча.

`.codex/Features/pipeline-advancement.md`

# Commands
`pwsh -NoProfile -File D:\tools\utils\business-module-scanner\maintenance\Invoke-ProjectCommand.ps1 D:\Aggregator\data-collection-platform pwsh -NoProfile -File tools/check.ps1 verify` is single source-verification entrypoint. It runs generated-contract drift checks, formatting, lint, strict typing, architecture boundaries, non-integration tests, and first campaign snapshot validation in order, and stops at first failure.

`.codex/Commands/COMMON_COMMANDS.md` holds the scoped `tools/check.ps1` steps, the scoped test run, campaign authoring commands, and the exact environment integration tests and Worker Gateway need.

Bootstrap: `uv sync --frozen --all-packages --dev`, then `python tools/git_hooks/configure.py`.
