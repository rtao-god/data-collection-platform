# Evidence processing

Цель: извлечь из сырого доказательства типизированные наблюдения с явными состояниями.

`evidence-processing/extraction-normalization` — контракты extraction request, extracted record, normalization profile, field observation и observation batch; детерминированное извлечение JSON-LD/microdata/RDFa и ограниченное извлечение контактов и адресов из HTML; типизированная нормализация значений; хост processing worker.

Цепочки:

Сырой артефакт → extraction request → извлечение с ограниченными evidence spans → extracted record → нормализация → field observation → observation batch → derived артефакт.

Границы:

- наблюдение явно `observed`/`missing`/`prohibited`/`invalid`; отсутствие не становится пустым успехом;
- нормализация телефона, URL/домена, email, адреса, денег, множеств и boolean с отрицанием; нераспознанное значение не подменяется нейтральным;
- извлечение детерминировано и ограничено по объёму доказательств;
- владельцы не создают кандидатов, ревью, качество или экспорт;
- worker запускает ровно одну настроенную capability (`extraction` или `normalization`), входы — scoped роли артефактов, выход — один верифицированный `derived_artifact`;
- worker не получает учётные данные PostgreSQL или S3.

Философия: каждое поле наблюдения должно указывать на доказательство. Derived артефакт — новый смысл, а не копия сырых байтов.
