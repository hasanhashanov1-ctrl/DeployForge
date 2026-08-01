# Демонстрация DeployForge за пять минут

Этот сценарий показывает главные возможности проекта без подготовки Python или Node.js
на Windows. Нужен только запущенный Docker Desktop в режиме Linux containers.

## 1. Запустить платформу

Из корня проекта:

```powershell
docker compose up --build -d
docker compose ps
```

Все постоянные сервисы должны перейти в состояние `healthy`. Затем откройте
<http://localhost:8000>.

## 2. Создать проект

Нажмите «Новый проект» и заполните форму:

| Поле | Значение |
|---|---|
| Название | `DigitalOcean Sample` |
| Slug | `deployforge-demo` |
| GitHub repository | `https://github.com/digitalocean/sample-dockerfile` |
| Ветка | `main` |
| Dockerfile | `Dockerfile` |
| Порт контейнера | `80` |

Если такой slug уже существует, используйте, например, `deployforge-demo-2`.

## 3. Запустить деплой

Нажмите «Запустить деплой» и сразу откройте вкладку «Логи». Панель покажет переходы:

```text
queued → cloning → building → starting → running
```

Build-лог поступает в реальном времени через SSE. После статуса «Работает» нажмите
«Открыть сервис»: приложение должно открыться по адресу
`http://deployforge-demo.localhost`.

## 4. Показать управление жизненным циклом

- «Остановить» завершает контейнер в фоновой задаче, сохраняя образ и историю.
- «Запустить» повторно поднимает сохранённую версию без новой сборки.
- «Откатить» в истории переключает сервис на выбранный предыдущий deployment.
- Во время нового деплоя кнопка «Отменить деплой» безопасно очищает кандидата и сохраняет
  предыдущую рабочую версию.

## 5. Показать настройки и безопасность

Во вкладке «Настройки» можно изменить GitHub-репозиторий, ветку и путь к Dockerfile,
а также добавить переменные окружения. Значение с отметкой «Секрет» после сохранения
маскируется в API и хранится в PostgreSQL в зашифрованном виде.

Важно проговорить ограничение MVP: worker имеет полный доступ к Docker Engine, поэтому
разворачивать следует только собственные или проверенные публичные репозитории.

## Быстрая техническая проверка

```powershell
docker build --target test -t deployforge-tests .
docker run --rm deployforge-tests
```

Отдельный тест настоящего Docker-контейнера:

```powershell
docker run --rm --user root `
  -e DEPLOYFORGE_RUN_DOCKER_TESTS=1 `
  -v /var/run/docker.sock:/var/run/docker.sock `
  deployforge-tests `
  pytest -p no:cacheprovider tests/test_docker_integration.py
```

## Остановить платформу

```powershell
docker compose down
```

PostgreSQL volume при этом сохраняется. Команда `docker compose down -v` дополнительно
удалит базу данных и должна использоваться только при намеренном полном сбросе.
