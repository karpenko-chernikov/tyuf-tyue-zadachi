# Как залить проект на GitHub (для Никиты)

Друг (`folomki`) использует **GitHub Desktop** — это удобная программа с кнопками. Вам тоже проще так.

## Шаг 1. Аккаунт на GitHub

1. Откройте https://github.com/signup  
2. Зарегистрируйтесь (email + пароль).  
3. Запомните **логин** (например `nikita-chernikov`).

## Шаг 2. Установите GitHub Desktop

1. Скачайте: https://desktop.github.com  
2. Установите и откройте.  
3. Войдите в свой GitHub-аккаунт (Sign in to GitHub.com).

## Шаг 3. Добавьте этот проект

1. В GitHub Desktop: **File → Add Local Repository…**  
2. Выберите папку:  
   `/Users/nikita/repos/tyuf-tyue-zadachi`  
3. Если спросит «create a repository» — можно **Create repository** (код уже с коммитом).

Либо: **File → New Repository** не нужен — проект уже готов.

## Шаг 4. Опубликуйте на GitHub

1. Нажмите **Publish repository** (справа вверху).  
2. Название: `tyuf-tyue-zadachi`  
3. **Снимите галочку** «Keep this code private», если хотите, чтобы другу было проще —  
   или оставьте **Private** (тогда друга нужно пригласить, см. шаг 5).  
4. Publish.

После этого появится ссылка вида:  
`https://github.com/ВАШ_ЛОГИН/tyuf-tyue-zadachi`

## Шаг 5. Добавить соавтора folomki

1. Откройте репозиторий на сайте GitHub.  
2. **Settings** → **Collaborators** (слева; может попросить пароль).  
3. **Add people** → введите: `folomki`  
4. Отправьте приглашение.

Он получит письмо / уведомление на GitHub и нажмёт **Accept**.

Можно также скинуть ему ссылку на репозиторий — он клонирует через GitHub Desktop:  
**File → Clone repository**.

## Что НЕ попадает на GitHub

- `.env` (пароли) — правильно, не светим  
- `data/zadachi.db` (ваши задачи) — тоже не в git  

Базу задач другу отдельно: скопируйте файл  
`/Users/nikita/repos/tyuf-tyue-zadachi/data/zadachi.db`  
и скиньте в Telegram / облако, если нужно перенести уже введённые задачи.

## Что сказать folomki

```
Репозиторий: https://github.com/ВАШ_ЛОГИН/tyuf-tyue-zadachi
Я добавил тебя collaborator (folomki).
Клонируй через GitHub Desktop.
Для запуска: см. README.md
Базу задач (если нужна) пришлю файлом zadachi.db → положить в data/
```

---

## Альтернатива через терминал (если освоите)

```bash
cd ~/repos/tyuf-tyue-zadachi
gh auth login
gh repo create tyuf-tyue-zadachi --private --source=. --remote=origin --push
gh api repos/ВАШ_ЛОГИН/tyuf-tyue-zadachi/collaborators/folomki -X PUT -f permission=push
```
