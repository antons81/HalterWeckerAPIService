# HalteWecker: покрытие городов, компактный fallback и безопасный deployment

Создал Anton, 10.10.2026.

## Границы выполненной работы

Подготовлено только локальное исправление. Commit, push, VPS pipeline, deployment,
изменение указателей, перезапуск и удаление production данных не выполнялись на
этом этапе. Вспомогательный запуск локального кода внутри production контейнера
был отклонён автоматической проверкой разрешений и не выполнялся.
Все команды ниже — план для отдельного разрешённого окна обслуживания.

## Изменения и обязательные проверки

- Hybrid обслуживает провайдеров через shards и остальные города через fallback.
  Исправлены registry, разрешение города, child stops и Finland contexts.
  Явный hybrid исключает автоматический переход в provider-only.
- Germany shard, уже необходимый RouteRecall, используется также HalteWecker.
  Runtime/build defaults включают 14 провайдеров. Docker image получает registry
  Germany; resolver поддерживает расположение `/app/config` внутри контейнера.
- Fallback импортирует Austria и остальные источники вне shard runtime. Germany и
  полностью покрытые shards источники повторно не импортируются. Для смешанных
  городов сохраняется полный feed. Импорт отмечает `fallbackProfile=hybrid-only`,
  `shardProviderIDs`, `excludedProviderIDs`; несогласованный runtime отвергается.
- Stephansplatz получает отправления дочерних платформ, если alias/parent
  не имеют собственных отправлений.
- Каждый production candidate содержит fallback из своего поколения, закреплённый
  SHA-256 и размером. Упаковка использует hardlink, а не вторую физическую копию.
- Проверяются все города manifest, все опубликованные stop IDs и их routing,
  обязательные source/city mappings, календари на сегодня и завтра, Berlin/VBB
  и пакетные расписания. Удаление города или потеря существовавшего расписания
  относительно проверенного rollback блокирует activation.
- Для каждого API города выполняется HTTP board через временный контейнер на
  candidate pointer. SQL witness с расписанием обязан дать непустой HTTP board.
  Пакетные города проверяются через HTTP отдельно. Три smoke stops дополняют этот
  набор: Wien Volkertplatz, Stephansplatz, Helsinki Lapinlahti.
- Candidate и rollback независимо проходят одинаковый consumer preflight на
  точном API image SHA. PASS остальных receipts не заменяет этот gate.
  Freeze/benchmark candidate без fallback не может пройти production activation.
- Активация использует проверенный SHA без build/pull. Параметры проверенного
  runtime сохраняются после чтения env files. Старый контейнер сохраняется.
  Смена image после preflight блокирует activation.
- RouteRecall сохраняет image/container IDs. При неизменном stop-data root
  выполняется readiness; при смене root разрешён только restart того же
  контейнера. Rebuild RouteRecall в nightly отсутствует.
- Успех оставляет `rollback` на проверенный baseline. При ошибке после переключения
  восстанавливаются baseline pointer/контейнеры и повторяется readiness.
  Activation dry-run не переключает указатели и не перезапускает контейнеры.
- Standalone static importer не может перезаписать immutable incremental release
  без candidate assembly и consumer preflight.

## Локальные результаты

| Проверки | Выполнения PASS |
| --- | ---: |
| Disk budget | 8 |
| Compact fallback/import | 5 |
| Provider capabilities/container layout | 6 |
| Все города / coverage contract | 18 |
| Static runtime | 29 |
| Nightly activation | 21 |
| Shared consumer preflight | 9 |
| Austria / parent-child stops | 5 |
| Static pipeline | 12 |
| Release assembler | 21 |
| Incremental pipeline | 70 |
| Berlin/VBB | 7 |
| Multi-provider API | 7 |
| HTTP API | 26 |
| Stop-data pipeline | 56 |
| Cleanup и published artifact retention | 86 |
| **Всего выполнений** | **386** |

Часть suites повторно включает импортированные TestCase; число обозначает
выполнения, а не уникальные test methods. Stop-data suite: 56 тестов / 304 сек.
Shell pipelines проверены на временных fixtures с mock GTFS/Docker; HTTP API
тесты запускают локальные временные серверы. `git diff --check`, `bash -n`, Python syntax и компиляция встроенного HTTP probe PASS.
Отрицательные сценарии включают non-pilot routing, отсутствие fallback,
неверное поколение/hash, потерю города/stop/provider mapping/расписания,
истёкший календарь, пустой HTTP board, неработоспособный rollback, stale runtime
без Germany, подмену pinned image/env contract и исчерпание дискового резерва.

Ранее выполненная read-only проверка существующего восстановленного релиза:
10 872 города = 10 856 API/VBB + 16 пакетных. Отдельный исправленный Python runtime
дал непустые ответы Wien (включая Stephansplatz), Helsinki, Bochum, Berlin,
Israel, Toronto, Chicago, Boston, Montreal, Oslo, Stockholm — 14 запросов.
Это предыдущая проверка с полным fallback, не подтверждение нового compact
candidate или Germany в новом Docker image. Все HTTP города, shared Germany,
фактический размер compact fallback и время полного nightly остаются обязательными
проверками перед будущей активацией. Локальные tests не заменяют этот этап.

## Дисковый бюджет

Уточнение пользователя: **во время сборки должно оставаться не меньше 20 ГБ**.
Код использует 20 GiB, что немного строже 20 десятичных GB. Это не ограничение
общего размера данных. Прежнее требование 103.4 GiB свободного места отменено:
повторная полная SQLite теперь не входит в production build.

Предварительная оценка складывает новую stop-data, предполагаемый fallback,
cache misses, временный workspace и запас. До измерения compact fallback
используется оценка 20 GiB; это оценка, не результат production сборки.
Тяжёлые этапы дополнительно контролируют фактическое свободное место каждые
0.25 сек и останавливают только свою группу процессов у 25 GiB, оставляя
5 GiB для buffered writes и завершения процесса. Порог ниже 20 GiB запрещён.
При нехватке места candidate не активируется; работающий API не останавливается.
Дисковые нагрузки других процессов также отражаются в этом измерении.

Последний подтверждённый аудит VPS: около 59.1 GiB свободно; действующая полная
SQLite занимает 36.8 GiB. Она сейчас единственная departures SQLite, поэтому
удалять её до замены и проверки полноценного rollback нельзя.
Размер compact fallback и реальный пик на VPS ещё не измерены. До первой сборки
нужно выполнить безопасный dry-run очистки и обеспечить прохождение disk guard.
Цель около 96 ГБ свободного места ещё не достигнута и не подтверждена.

## План после отдельного разрешения — команды в Bash на VPS

### 1. Согласованное окно, baseline, locks и безопасная очистка

Работающий pipeline не прерывать. После разрешения временно остановить nightly
и static timers, сохранив их исходное состояние, и дождаться свободных locks.
Не запускать Git на VPS; wrapper и его fetch/merge workflow не менять.
Синхронизировать только согласованный diff отдельно разрешённым способом.

```bash
cd /srv/haltewecker/pipeline/HalterWeckerAPIService
export DATA_ROOT=/srv/haltewecker/data
export BASELINE="$(readlink -f "$DATA_ROOT/current-release")"
export BASELINE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["releaseID"])' "$BASELINE/release.json")"
export RUNTIME_PROVIDERS='israel-mot,ttc-surface,ttc-subway,norway,sweden,poland-warsaw,poland-wkd,511-bay-area,australia-translink-seq,australia-transport-nsw,cta-chicago,mbta-boston,stm-montreal,germany'
export BUILD_PROVIDERS="$RUNTIME_PROVIDERS"
df -Pk "$DATA_ROOT"
docker inspect --format '{{.Name}} {{.Image}} {{.Id}}' static-departures-api routerecall-api
systemctl status haltewecker-stop-data.service haltewecker-stop-data.timer --no-pager
flock -n /run/lock/haltewecker-stop-data.lock true
flock -n /run/lock/haltewecker-static-departures.lock true
HALTEWECKER_CLEANUP_DRY_RUN=1 bash ops/haltewecker-cleanup.sh
```

Получить конкретный KEEP/DELETE allowlist и физические reclaimable bytes по
уникальным device/inode/nlink. Сохранить current/previous/rollback/pilot,
транзитивные manifest refs, runtime roots, open/mmap files и locks обоих apps.
Broken/unknown references означают KEEP/CLEANUP_SKIPPED. Перед каждым удалением
повторить проверку inode и ссылок. Удаление выполняется только после отдельного
разрешения на этот точный список. `docker system prune` и массовое удаление
старых releases по возрасту в план не входят. Если достаточного места нет — STOP.

### 2. Новый API image и проверенный baseline без переключения данных

Применить локальную systemd runtime-конфигурацию и согласовать env files:
14 провайдеров, hybrid=1, provider-only=0, pointers/database в current-release.
Daemon reload и изменения серверной конфигурации требуют отдельного разрешения.

```bash
docker build -f services/static-departures.Dockerfile -t haltewecker-static-departures:coverage-20261010 .
export STATIC_IMAGE="$(docker image inspect --format '{{.Id}}' haltewecker-static-departures:coverage-20261010)"
python3 scripts/validate_shared_release_consumers.py \
  --reference-only --release "$BASELINE" --rollback-release "$BASELINE" \
  --static-image "$STATIC_IMAGE" \
  --build-providers "$BUILD_PROVIDERS" --runtime-providers "$RUNTIME_PROVIDERS"
```

Нужен PASS всех городов/трёх smoke stops и RouteRecall Germany routes
Berlin 100, Wuppertal 635. Production/geometry pointers обязаны остаться прежними.
Это временные readonly контейнеры; production контейнеры не останавливаются.
При любом FAIL — STOP. Важна реальная проверка Germany shard внутри этого image.

После PASS и отдельного разрешения активировать новый API image на том же
полном baseline; это создаёт рабочий API и rollback до сборки compact данных.

```bash
STATIC_DEPARTURES_IMAGE="$STATIC_IMAGE" \
HALTEWECKER_RUNTIME_PROVIDER_IDS="$RUNTIME_PROVIDERS" \
HALTEWECKER_STATIC_DEPARTURES_RUNTIME_MODE=hybrid \
HALTEWECKER_STATIC_DEPARTURES_PROVIDER_RUNTIME=0 \
HALTEWECKER_STATIC_DEPARTURES_HYBRID_RUNTIME=1 \
HALTEWECKER_STATIC_DEPARTURES_HYBRID_PROVIDERS="$RUNTIME_PROVIDERS" \
HALTEWECKER_STATIC_DEPARTURES_HYBRID_RELEASE_POINTER=/data/current-release \
DEPARTURES_DATABASE=/data/current-release/departures.sqlite \
STATIC_DATA_ROOT=/data/current-release/stop-data \
READINESS_ONLY=1 RELEASE_ID="$BASELINE_ID" \
flock -n /run/lock/haltewecker-stop-data.lock scripts/run_static_departures_pipeline.sh
python3 scripts/active_release_readiness.py haltewecker \
  --container static-departures-api --release-id "$BASELINE_ID" --providers "$RUNTIME_PROVIDERS"
```

Сверить image SHA и baseline pointer. RouteRecall container/image не меняются.

### 3. Compact candidate, измерение размера и полный preflight

```bash
scripts/run_stop_data_pipeline.sh --incremental-no-activate
```

Зафиксировать фактический candidate ID и minimum free bytes из лога. Никаких
frozen/proof overrides. Проверить metadata `hybrid-only`, отсутствие повторного
Germany/pilot import, полное покрытие и actual disk usage. FAIL оставляет baseline.

```bash
export CANDIDATE_ID='ФАКТИЧЕСКИЙ_ID_ИЗ_ЛОГА'
export CANDIDATE="$DATA_ROOT/releases/incremental/$CANDIDATE_ID"
python3 scripts/validate_shared_release_consumers.py \
  --release "$CANDIDATE" --rollback-release "$BASELINE" \
  --static-image "$STATIC_IMAGE" \
  --build-providers "$BUILD_PROVIDERS" --runtime-providers "$RUNTIME_PROVIDERS"
```

Требуются отдельные PASS candidate и verifiedRollback с неизменными production
и geometry pointers, releaseID/fallbackReleaseID/hash, HTTP по каждому городу,
smoke stops и обоим RouteRecall routes. Истёкший или неработоспособный baseline
означает STOP; предыдущий релиз по номеру нельзя объявить здоровым rollback.

### 4. Активация и проверка восстановления

После отдельного разрешения:

```bash
scripts/run_stop_data_pipeline.sh --activate-existing-candidate "$CANDIDATE_ID"
```

Команда повторяет gates до pointer switch. После успеха проверить current=candidate,
rollback=baseline, оба API, smoke stops, полное покрытие и точные RouteRecall IDs.
В согласованном окне выполнить реальный rollback drill к baseline, повторить
readiness обоих apps и затем повторно активировать candidate тем же штатным
activator. Это отдельные разрешённые production действия, сейчас они не сделаны.
FAIL восстановления блокирует дальнейший rollout и очистку.

### 5. Компактный rollback, очистка и следующий nightly

Полный baseline сохранить до появления второго независимо проверенного compact
релиза, чтобы current и rollback оба были работоспособны без 36.8 GiB базы.
Если для второй сборки места не хватает, сначала согласовать очистку безопасных
неиспользуемых объектов; не снимать rollback и не понижать резерв.

После двух compact релизов и rollback drill пересчитать ссылки, open/mmap/locks
и физические bytes. Только тогда включить старую полную SQLite и невостребованные
release/cache/workspace/container объекты в точный allowlist удаления.
Сохранить текущий/rollback API image и оба RouteRecall dependencies.
После удаления — `df -Pk`, health, smoke, RouteRecall IDs и all-city preflight.
Фактическое достижение около 96 ГБ проверяется `df`, не суммой логических `du`.

Возобновить только ранее активные timers. Следующий штатный nightly должен
пройти полный candidate/rollback preflight до activation и те же disk guards.
Проверить утренние API обоих apps и duration/минимум свободного места.
Существующая reference/inode-aware retention должна сохранять current+rollback
и удалять только неиспользуемые объекты после TTL. Любой отказ build/coverage
оставляет проверенный релиз активным и требует разбор причины по логам.
