# Перенос jwu + Hermes оркестратора на другую машину

## Что в репозитории Jwu (в git)

| Путь | Назначение |
|------|------------|
| `.hermes/skills/jwu-*/` | Доменные workflow для Hermes (ревью, todo, CI, …) |
| `.hermes/skills/jwu-orchestrator/` | Как оркестрировать работу |
| `src/jwu/skills/jwu-hermes-orchestrator/` (в wheel после `pipx install`) | **Gateway-скрипты** (`worker_launch`, `worker_relay`, `worker_archive`) |
| `.hermes/skills/jwu-hermes-orchestrator` | Симлинк на bundled (каталог Hermes-проекта Jwu) |

Профильный скил `jwu-workspace-operations` в `~/.hermes/skills/productivity/` — обёртка Hermes; на новой машине его можно поставить симлинком (см. ниже) или держать копию.

## Минимум на новом Mac

1. **Hermes Desktop** (или gateway + CLI) — см. [Hermes docs](https://hermes-agent.nousresearch.com/docs).
2. **jwu в pipx:** `pipx install git+https://github.com/ArtjomKotkov/jwu.git` (или clone + `pipx install --force .`).
3. **`jwu install`** — MCP и скиллы; в `~/.claude/skills/jwu-hermes-orchestrator/` попадут и `scripts/`.
4. **Hermes Desktop:** симлинк на каталог скилла (клон или pipx venv):

```bash
# из клона
SKILL="$PWD/src/jwu/skills/jwu-hermes-orchestrator"
# или после pipx: SKILL="$(python3 -c 'import importlib.resources as r; print(r.files("jwu")/"skills"/"jwu-hermes-orchestrator")')"
```

5. **Скиллы Hermes из репо (симлинки в профиль):** из корня клона Jwu:

```bash
JWU_ROOT="$PWD"   # каталог с .hermes/skills
PROFILE_SKILLS="$HOME/.hermes/skills/productivity"
mkdir -p "$PROFILE_SKILLS"

# Оркестратор + gateway-утилиты (один симлинк)
ln -sfn "$JWU_ROOT/src/jwu/skills/jwu-hermes-orchestrator" \
  "$PROFILE_SKILLS/jwu-hermes-orchestrator"

# Опционально: весь каталог Hermes-скиллов jwu (если Desktop не подхватывает project skills)
for d in "$JWU_ROOT/.hermes/skills"/jwu-*; do
  name=$(basename "$d")
  ln -sfn "$d" "$PROFILE_SKILLS/$name"
done
```

5. В Desktop открой **проект** с корнем воркспейса (не только папку Jwu, если work — отдельный каталог). Чаты оркестратора — в том проекте, cwd = workdir репозитория.

## Запуск worker_launch с оркестратора

```bash
SKILL="$JWU_ROOT/.hermes/skills/jwu-hermes-orchestrator"
"$SKILL/scripts/run_with_gateway.sh" \
  "$SKILL/scripts/worker_launch.py" \
  --title "24.11 · …" --brief-file /path/to/brief.md
```

`HERMES_SESSION_ID` должен быть задан в среде чата оркестратора (Desktop делает это сам при выполнении команд из чата).

## Проверка

```bash
"$SKILL/scripts/run_with_gateway.sh" \
  "$SKILL/scripts/worker_launch.py" --title "probe" --dry-run
```

В JSON должны быть `model`, `provider`, `cwd` как у текущего оркестратора.
