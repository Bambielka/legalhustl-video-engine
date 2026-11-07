# Коригування репозиторію SENTINEL LAW Motion Engine V4.2

Це додатковий патч до вже завантаженої V4.2. Основний рендерер, тести, README та демонстраційний JSON повторно завантажувати не потрібно.

## Файли для внесення в GitHub

У репозиторії `Bambielka/legalhustl-video-engine`, гілка `main`:

### Замінити наявні файли

1. `.github/workflows/render.yml`
2. `MANIFEST_SHA256`

### Додати відсутні файли

3. `CHANGELOG_V4_2.md`
4. `INSTALL_V4_2.md`

Зберігай шляхи файлів саме так, як зазначено вище. Особливо важливо не створити `render.yml` у корені репозиторію: він має залишатися в `.github/workflows/`.

## Що змінено у workflow

У фільтрах `push.paths` та `pull_request.paths` застарілий шлях `sample-story.json` замінений на фактичний `sample-news-storyboard.json`.

Тепер зміни демонстраційного сценарію V4.2 також запускають перевірки GitHub Actions. Розклад запуску кожні 10 хвилин, `workflow_dispatch`, Python 3.13, 29 тестів та обробка черги не змінені.

Важливо: подія `push` запускає тільки тестовий job. Job `Render queued videos` на `push` має бути `skipped`, щоб звичайне оновлення файлів не забирало реальні завдання з Google Sheets. Рендеринг черги запускається за розкладом або вручну через `workflow_dispatch`.

## Перевірка після завантаження

1. Відкрий GitHub → Actions → `SENTINEL LAW Motion Engine`.
2. Переконайся, що після оновлення workflow з'явився новий запуск із подією `push`.
3. `Renderer regression and MP4 smoke tests` має завершитися зі статусом `success`.
4. У логах тестів очікується `Ran 29 tests ... OK`.
5. `Render queued videos` на події `push` має бути `skipped`.

Цей патч не змінює `renderer.py`, `tests/test_renderer.py`, `worker.py`, `requirements.txt`, Apps Script, GitHub Secrets або формат завдань у Google Sheets.
