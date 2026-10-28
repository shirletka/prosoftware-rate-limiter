# Rate limiter со скользящим окном

`POST /v1/echo` возвращает тело запроса как есть, байт в байт и с тем же `Content-Type`. Каждый ключ из `Authorization: Bearer <key>` может сделать не больше N запросов за T секунд. Окно скользящее, состояние лежит в Redis, проверка и запись выполняются одним Lua-скриптом.

## Запуск

```bash
docker compose up --build
```

API поднимется на `localhost:8000`. В compose по умолчанию 5 запросов за 10 секунд, чтобы лимит было легко увидеть руками:

```bash
for i in $(seq 6); do
  curl -s -o /dev/null -w "%{http_code} remaining=%header{x-ratelimit-remaining} retry-after=%header{retry-after}\n" \
    -X POST localhost:8000/v1/echo -H 'Authorization: Bearer demo' \
    -H 'Content-Type: application/json' -d '{"ping": 1}'
done
```

```
200 remaining=4 retry-after=
200 remaining=3 retry-after=
200 remaining=2 retry-after=
200 remaining=1 retry-after=
200 remaining=0 retry-after=
429 remaining=0 retry-after=10
```

## Настройки

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `RATE_LIMIT_REQUESTS` | 10 (в compose 5) | N, сколько запросов разрешено в окне |
| `RATE_LIMIT_WINDOW_SECONDS` | 60 (в compose 10) | T, длина окна; можно дробное, точность 1 мс |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `API_KEYS` | пусто | Список ключей через запятую. Пусто — принимается любой ключ, лимит считается для каждого отдельно. В шлюзе ключ обычно проверяет слой авторизации до лимитера, а здесь allowlist нужен, чтобы лимит нельзя было обойти сменой токена |
| `RATE_LIMIT_FAIL_OPEN` | `false` | Что делать, если Redis недоступен: `false` — 503, `true` — пропустить запрос и записать предупреждение в лог |
| `REDIS_TIMEOUT_SECONDS` | 0.5 | Таймаут подключения и ответа Redis. По умолчанию redis-py ждёт 5 секунд, и каждый запрос висел бы столько же, прежде чем получить 503 |

## Ответы

- `200` — тело запроса с его `Content-Type` и заголовки `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
- `429` — те же заголовки и `Retry-After`. Оба значения в секундах относительно текущего момента, округлены вверх: клиент, который подождёт `Retry-After` секунд, гарантированно пройдёт. Это верно и после того, как лимит уменьшили на редеплое и в окне лежит больше запросов, чем теперь разрешено: тогда ждать нужно, пока уйдёт не самый старый запрос, а столько, чтобы осталось меньше N (`test_lowered_limit_waits_for_enough_requests_to_expire`).
- `401` — нет заголовка, схема не Bearer или ключа нет в `API_KEYS`. Отдаётся с `WWW-Authenticate: Bearer`.
- `503` — Redis недоступен и `RATE_LIMIT_FAIL_OPEN=false`.

## Тесты

Одной командой, против настоящего Redis:

```bash
docker compose --profile test run --rm --build tests
```

Без Docker те же тесты идут на fakeredis (Lua там исполняется через lupa), кроме теста на гонку: fakeredis выполняет команды строго по одной, и неатомарная реализация прошла бы его так же. Этот тест запускается только против настоящего Redis.

```bash
uv run pytest
```

Вывод на моей машине (Redis 7.4 в compose):

```
tests/test_api.py::test_echoes_the_body PASSED
tests/test_api.py::test_echoes_non_json_bodies_byte_for_byte PASSED
tests/test_api.py::test_reports_remaining_quota PASSED
tests/test_api.py::test_returns_429_with_retry_after_when_limit_exceeded PASSED
tests/test_api.py::test_accepts_requests_again_after_the_window_moves PASSED
tests/test_api.py::test_limits_are_per_key PASSED
tests/test_api.py::test_requires_bearer_token[headers0] PASSED
tests/test_api.py::test_requires_bearer_token[headers1] PASSED
tests/test_api.py::test_requires_bearer_token[headers2] PASSED
tests/test_api.py::test_rejects_keys_outside_the_allowlist PASSED
tests/test_api.py::test_healthz_reports_redis_outage PASSED
tests/test_api.py::test_redis_outage[False-503] PASSED
tests/test_api.py::test_redis_outage[True-200] PASSED
tests/test_limiter.py::test_allows_requests_up_to_the_limit PASSED
tests/test_limiter.py::test_rejects_when_limit_is_reached PASSED
tests/test_limiter.py::test_window_boundary_is_exclusive_for_the_oldest_request PASSED
tests/test_limiter.py::test_window_slides_one_request_at_a_time PASSED
tests/test_limiter.py::test_rejected_requests_do_not_push_the_window PASSED
tests/test_limiter.py::test_keys_are_independent PASSED
tests/test_limiter.py::test_bucket_expires_with_the_window PASSED
tests/test_limiter.py::test_raw_api_key_is_not_stored PASSED
tests/test_limiter.py::test_lowered_limit_waits_for_enough_requests_to_expire PASSED
tests/test_limiter.py::test_concurrent_requests_never_exceed_the_limit PASSED
============================== 23 passed in 0.37s ==============================
```

Время в тестах не настоящее: лимитер получает часы снаружи, и тест двигает их на нужное число миллисекунд. Поэтому граница окна проверяется точно (за 1 мс до конца окна запрос отклонён, ровно на границе проходит), и ни один тест не спит.

## Как устроено

```
app/
  sliding_window.lua   весь алгоритм, 30 строк
  limiter.py           обёртка над скриптом: часы, хеш ключа, разбор ответа
  main.py              FastAPI: авторизация, зависимость rate_limit, /v1/echo
  config.py            настройки из env
```

На каждый ключ в Redis заведён sorted set: элемент — id запроса, score — время в миллисекундах. Скрипт удаляет всё, что старше `now - T`, считает оставшееся, и если места есть, добавляет текущий запрос. В ответ отдаёт время самого старого запроса в окне: из него получаются и `Retry-After`, и `X-RateLimit-Reset`.

## Решения и компромиссы

**Sorted set, а не счётчик.** Есть два популярных способа: лог запросов (sorted set) и «скользящий счётчик», который смешивает два фиксированных окна с весами. Счётчик занимает O(1) памяти, но это приближение: на стыке окон он пропускает лишние запросы или режет лишние. В критериях стоит «корректность на границе окна», поэтому я выбрал точный вариант. Цена — до N элементов на ключ. При лимитах в десятки и сотни запросов это килобайты. Если когда-то понадобится 100 000 запросов в минуту на ключ, я бы перешёл на счётчик и честно описал погрешность.

**Lua, а не pipeline.** В pipeline с `MULTI` нельзя принять решение посередине: сначала надо узнать `ZCARD`, а потом решить, делать ли `ZADD`. Можно добавлять всегда и удалять при отказе, но тогда отказанные запросы на мгновение занимают место, и параллельный запрос может получить 429 зря. Скрипт выполняется в Redis атомарно, поэтому `test_concurrent_requests_never_exceed_the_limit` (100 одновременных запросов при лимите 10) на настоящем Redis всегда даёт ровно 10. `register_script` отправляет скрипт через `EVALSHA` и сам загружает его при `NOSCRIPT` после рестарта Redis.

**Отказанные запросы не записываются.** Иначе клиент, который долбит API в цикле, никогда не выйдет из бана: каждый его 429 продлевал бы окно. Это проверяет `test_rejected_requests_do_not_push_the_window`.

**Время приходит из приложения.** Так время в тестах подменяется без моков. Если инстансов API несколько, их часы синхронизированы через NTP с точностью до миллисекунд, этого хватает для окон в секунды. Если понадобится строже, в скрипте одна строка: брать `redis.call('TIME')`, тогда часы будут одни на всех.

**Ключ хранится как SHA-256.** В Redis лежит `ratelimit:<sha256>`, а не сам ключ. Дамп Redis или `MONITOR` не раскрывают рабочие ключи клиентов. TTL ключа равен окну, так что брошенные ключи сами исчезают из памяти.

**Что делать без Redis — решает настройка.** Для API, где лимит защищает от перерасхода денег (например, прокси к платной модели), правильнее 503. Для API, где лимит нужен для честного распределения нагрузки, иногда лучше пропустить запрос, чем уронить продукт. Поэтому `RATE_LIMIT_FAIL_OPEN`, по умолчанию закрыто.

**`X-RateLimit-Reset` в секундах «через сколько», а не unix-время.** Так же считает черновик IETF `RateLimit` headers. GitHub отдаёт epoch, поэтому формат указан здесь явно.

## Если это пойдёт в LLM-шлюз

Лимит по числу запросов для LLM грубоват: один запрос на 200 токенов и один на 100 000 стоят по-разному. Скрипт легко научить весу: элемент sorted set хранит не только id, но и число токенов (`id:tokens`), а вместо `ZCARD` скрипт суммирует веса в окне. Тогда тот же механизм ограничит токены в минуту на ключ или на модель. Для Redis Cluster ключи одного клиента стоит держать в одном слоте через hash tag `ratelimit:{tenant}:...`.
