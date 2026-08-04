<p align="center">
  <img src="assets/logo.png" alt="inkling" width="180">
</p>

<h1 align="center">inkling</h1>

<p align="center">Твой Obsidian, подключённый к Claude Code: заметки всплывают сами, а исследования пишутся обратно в хранилище.</p>

---

## Что это

Заметки обычно лежат мёртвым грузом. Ты записал что-то полгода назад, потом забыл, что записывал, и решаешь ту же задачу заново. Проблема не в поиске: чтобы найти заметку, надо вспомнить, что она существует.

inkling убирает этот шаг. Хук читает каждый твой запрос, сверяет его с тегами и краткими описаниями всех заметок и подкладывает агенту те, что подходят. Заметка приходит в тот момент, когда она нужна, а не когда ты про неё вспомнил.

Обратное направление работает так же. Скилл исследования сначала смотрит в хранилище, потом уже в интернет, а найденное сохраняет заметкой. Один и тот же вопрос оплачивается один раз.

Книги разбираются дешёвыми агентами-разведчиками: книга режется на куски, разведчики читают их параллельно, и в дорогой контекст попадают только выжимки. На выходе черновые заметки о концепциях.

И последнее: под конец длинной сессии система напоминает, что стоит сохранить то, что было понято по дороге. Она ничего не пишет сама. Решение всегда за тобой.

## Как это выглядит в работе

Всё, что ниже, — настоящий вывод, снятый с приёмочного прогона, который ты можешь запустить сам. Длинные служебные шапки урезаны и помечены `[...]`.

### Заметка, о которой ты забыл

Спрашиваешь про повторные попытки к тормозящему сервису. До того, как агент начнёт отвечать, ему подкладывают:

```
[RECALL] Wiki notes matched by your prompt tags. For each: read the summary and decide if it
enriches the current task. [...]
  - circuit-breaker: A retry without a circuit breaker turns one slow dependency into a full
    outage: every caller piles on while the callee is already saturated. Break the circuit
    after N consecutive failures and probe with a single request.
    → /vault/patterns/circuit-breaker.md
```

Заметку писал ты. Никто её не искал.

Находится она вот из-за этой записи, которую собрал компилятор. Это весь документ, по которому идёт ранжирование, и тела заметки в нём нет:

```json
{
  "name": "circuit-breaker",
  "path": "patterns/circuit-breaker.md",
  "tags": ["circuit-breaker", "retry", "resilience", "timeout", "outage"],
  "summary": "A retry without a circuit breaker turns one slow dependency into a full outage:
              every caller piles on while the callee is already saturated. Break the circuit
              after N consecutive failures and probe with a single request."
}
```

### То, что ты уже делал однажды в этом проекте

Вторая рельса хранит не знание, а порядок действий. Спрашиваешь, как пересобрать индекс, и получаешь последовательность, которая здесь уже срабатывала, с пометкой о том, сколько за ней доказательств:

```
[PROCEDURAL] Action sequence(s) that worked in this project before. [...]
  - rebuild-index-after-editing-notes: [worked once] an edited note does not show up in recall -
    an edited note stays invisible until the index is recompiled
    → .claude/procedural.md#rebuild-index-after-editing-notes
```

`[worked once]` — это база доказательств, и она говорит, насколько сильно опираться. Один успех это гипотеза, которой повезло; несколько сессий делают её паттерном. Законом не становится ни то, ни другое.

### Момент, который стоит сохранить

Ты говоришь, что на чём-то остановился. Рельса захвата это замечает и предлагает, ничего не записывая:

```
[CAPTURE] The user's message looks capture-worthy. These are SUGGESTIONS - act ONLY if the
content is genuinely durable, and never auto-write a note without the user. [...]
- This looks like a DECISION. If it is durable, meaning it shapes future work, record it
  together with the reason behind it so the same choice is not reopened in a month.
```

В конце сессии сборщик перечитывает твои сообщения и складывает то, что живая подсказка пропустила. Кандидаты ждут в `.claude/capture_inbox.md`, пока ты не запустишь `/capture-review`. Без тебя заметкой не станет ничего.

### Когда кончается место

Длинная сессия рано или поздно меняет понятое на свободное место. Перед этим приходит:

```
[CAPTURE-DUE] Context is about 85% full (170k of 200k). This is the point where something
learned in this session gets traded for space rather than remembered.
What to do, in this order:
1. Finish the user's current request first. Do not break off mid-step for bookkeeping.
2. Then sweep THIS session for what would cost real time to re-derive: a decision and the
   reason behind it, a gotcha with the symptom that gives it away, an approach tried and
   rejected. Skip anything that only matters until this task ends.
3. Offer the shortlist to the user, or run /capture-review if candidates are staged.
If nothing durable happened, say so in one line and move on.
```

Размер окна берётся из идентификатора модели, поэтому сессия с миллионом токенов запаса не получает предупреждение на шестой части своей ёмкости. Показание без правила — украшение, поэтому блок говорит, что делать, а не только насколько всё заполнено.

### Когда ломается сама рельса

Вот это и есть та часть, которой стоит доверять. Если индекс не читается, система скажет об этом, а не промолчит, притворившись честным промахом:

```
[RAIL-BROKEN] A recall corpus failed to load. This is a BUG, not an empty corpus - the affected
rail matched nothing because it could not read its index, not because nothing matched. [...]
  - wiki (Layer 5): parse index_compiled.json: invalid character 'b' looking for beginning of
    object key string
```

Тишина означает «искал и не нашёл». Она никогда не означает «не искал».

## Установка

Самый простой путь: скажи своему агенту прочитать `AGENTS.md` в этом репозитории. Там лежит полная инструкция по установке, написанная для него, а не для тебя. Он разложит файлы, пропишет хуки, прогонит приёмочные тесты и объяснит по-человечески, что получилось.

Руками — три шага:

```sh
git clone https://github.com/blackboostervoid/inkling.git && cd inkling
export CLAUDE_WIKI_PATH="/путь/к/твоему/Obsidian/vault/"
CONFIG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
cp -r skills agents agent-memory hooks scripts "$CONFIG/"
```

Потом слей `settings.example.json` в свой `settings.json` и проверь, что всё живо:

```sh
python3 tests/acceptance.py
```

Тест собирает одноразовую песочницу во временной папке, гоняет по ней обе рельсы и печатает по строке на проверку. Твоё хранилище он не трогает.

Нужны Python 3.9 и Go — вторым собираются два хука, которые работают на каждом запросе и поэтому написаны не на Python.

## Как писать заметки, чтобы они находились

Одно правило важнее всех остальных: **поиск читает имя файла, теги и `summary`, а тело заметки не читает никогда**.

```yaml
---
tags: [circuit-breaker, retry, resilience, timeout]
summary: >
  Повтор без размыкателя превращает один медленный сервис в полный отказ:
  все вызывающие наваливаются на уже перегруженного.
---
```

Теги пиши словами, которыми задаёшь вопросы, а не словами, которыми названа архитектура. В `summary` ставь вывод, а не тему: «повтор без размыкателя роняет систему» находится и оценивается, «про повторы и таймауты» — нет.

Полностью формат описан в [VAULT_CONTRACT.md](VAULT_CONTRACT.md), формат рецептов для второй рельсы — в [RECIPE_FORMAT.md](RECIPE_FORMAT.md).

## Чего он не делает

Он не ищет по телу заметок. Не пишет заметки сам. Не ускоряет работу агента. Он делает ровно одно: нужная заметка появляется в нужный момент.

## Лицензия

MIT.
