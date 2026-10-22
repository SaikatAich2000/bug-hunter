<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.png">
    <img alt="Bug Hunter: self-hosted bug, requirement and task tracking" src="docs/assets/banner-light.png" width="100%">
  </picture>
</p>

<p align="center">
  <a href="LICENSE.txt"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-2ea44f"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776ab?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white">
  <img alt="React 19" src="https://img.shields.io/badge/React-19-61dafb?logo=react&logoColor=black">
  <img alt="PostgreSQL 16" src="https://img.shields.io/badge/PostgreSQL-16-336791?logo=postgresql&logoColor=white">
  <img alt="Docker Compose" src="https://img.shields.io/badge/Docker-Compose-2496ed?logo=docker&logoColor=white">
  <img alt="Self-hosted" src="https://img.shields.io/badge/self--hosted-yes-67c1f5">
</p>

<p align="center">
  <b>Track bugs, requirements and tasks. Plan sprints. Own your data.</b><br>
  One Docker Compose stack: FastAPI, PostgreSQL and React. No outside login service needed.
</p>

<p align="center">
  <img alt="A tour of Bug Hunter: work items, sprints, events, analytics, reports and audit trail" src="docs/assets/tour.gif" width="860">
</p>

---

## ✨ What you get

| | | |
|---|---|---|
| 🐞 **Work items**<br>Bugs, requirements, tasks, stories and epics in one list. Filters, bulk edit, Excel import. | 🏁 **Sprints & Agile**<br>Ranked backlog, sprint board, epics, burndown and velocity. | 📊 **Analytics & reports**<br>Live charts and a report builder that exports to Excel. |
| 📅 **Events**<br>Group items for a stand-up or a review. | 🔔 **Notifications**<br>In-app bell, email digest and push, each linking to the item. | 🛡️ **Audit trail & sessions**<br>See who changed what, and log out any device. |
| 🕵️ **Sleuth assistant**<br>Ask in plain English. Runs on your server by default. | 🐙 **Git branches**<br>Create a feature branch for a User Story in one click. | 🌗 **Light and dark**<br>Responsive layout that respects reduced motion. |

<details>
<summary><b>See it in light and dark</b></summary>
<br>
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screens/analytics-dark.webp">
    <img alt="Analytics view" src="docs/assets/screens/analytics-light.webp" width="49%">
  </picture>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screens/sprints-dark.webp">
    <img alt="Sprints view" src="docs/assets/screens/sprints-light.webp" width="49%">
  </picture>
</p>
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screens/list-dark.webp">
    <img alt="Work items list" src="docs/assets/screens/list-light.webp" width="49%">
  </picture>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screens/audit-dark.webp">
    <img alt="Audit trail" src="docs/assets/screens/audit-light.webp" width="49%">
  </picture>
</p>
</details>

## 🚀 Quick start

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Docker Engine and Compose v2).

```bash
git clone https://github.com/<your-org>/bug-hunter.git
cd bug-hunter
cp .env.example .env     # set APP_VERSION, POSTGRES_PASSWORD and BOOTSTRAP_ADMIN_* at least
./deploy.sh              # builds, starts PostgreSQL, then the app
```

Open <http://localhost:8765> and sign in with the `BOOTSTRAP_ADMIN_*` account from `.env`.
To stop: `./down.sh`. Your data stays in the `bugtracker_pgdata` volume.

> 💡 **No Docker?** Run `bash scripts/run_local.sh` (or `.\scripts\run_local.ps1` on Windows). It uses SQLite and needs no Node.
> See [Local development](docs/development.md).

## 🧭 How it fits together

```mermaid
flowchart LR
    W["🌐 Web app<br>React 19 + Vite"] --> API
    subgraph Server["Bug Hunter (one container)"]
      API["⚙️ FastAPI<br>REST API + static files"]
      SL["🕵️ Sleuth<br>rules, classifier, optional LLM"]
      JOBS["⏱️ Daily jobs<br>digest and retention"]
    end
    API --> SL
    API --> DB[("🐘 PostgreSQL 16<br>data and attachments")]
    JOBS --> DB
    API -. "optional" .-> EXT["✉️ SMTP · 🔔 FCM push · 🐙 GitHub"]
```

One database holds everything, including attachments, so **one backup saves it all**.
More in [Architecture](docs/architecture.md).

### 👥 Roles at a glance

| Role | Can do |
|---|---|
| 👑 **Admin** | Everything: people, deletes, all projects. |
| 🧑‍💼 **Manager** | Edit any item or event, run sprints and boards. Cannot delete items or make admins. |
| 🙋 **User** | Create and edit bugs, stories and sub-tasks, move cards, view reports. |

Managers and users only see the projects they belong to.

## 🏁 Sprints, the Scrum way

```mermaid
stateDiagram-v2
    [*] --> Backlog: create issue
    Backlog --> Planned: add to a sprint
    Planned --> Active: start sprint
    Active --> Done: move to the last column
    Active --> Backlog: sprint completes, not done
    Done --> [*]
```

Agile is off until a manager clicks **Set up Scrum board**. Nothing is changed or deleted when you do.
Details: [Sprints and Agile](docs/agile.md).

## 🕵️ Sleuth, the assistant

<p>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/sleuth-dark.svg">
    <img alt="Sleuth logo" src="docs/assets/sleuth-light.svg" width="72" align="left" hspace="12">
  </picture>
  Ask <i>"show open bugs assigned to alice"</i>, <i>"how many critical bugs are in PROD?"</i>
  or <i>"close bug 5"</i>. Every change asks you first and is written to the audit trail.
  By default Sleuth runs fully on your server and sends nothing out.
</p>

<br clear="left">

```mermaid
flowchart LR
    Q["💬 Your question"] --> R["1 Rules<br>fast, exact"]
    R -->|not sure| C["2 Classifier<br>TF-IDF"]
    C -->|not sure| LL["3 Local LLM<br>optional"]
    LL -->|not sure| CL["4 Cloud LLM<br>optional, off by default"]
    R --> A["✅ Answer or confirmed action"]
    C --> A
    LL --> A
    CL --> A
```

More in [Sleuth](docs/sleuth.md).

## 🐙 Git feature branches

```mermaid
flowchart LR
    S["📘 User Story #42<br>Dark mode for settings"] -->|Create branch| G["🐙 GitHub"]
    G --> B["feature_42_dark-mode-for-settings"]
    B -->|Remove branch| X["🗑️ Deleted, kept as history"]
```

Off by default. Only User Stories get branches, and only the exact branch Bug Hunter created can be removed.
Setup: [Git integration](docs/git-integration.md).

## ⚙️ Settings that matter

Everything comes from environment variables. Copy [`.env.example`](.env.example); each line is explained there.

| Setting | Default | What it does |
|---|---|---|
| `APP_VERSION` | required | Version shown in the UI and at `/api/health`. |
| `APP_ENV` | development | Set `production` to turn on strict start-up checks. |
| `SESSION_SECRET` | blank | Signs session cookies. Use a long random value in production. |
| `EMAIL_BACKEND` | `console` | `console`, `smtp` or `disabled`. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | blank | Send traces, metrics and logs to SigNoz or any OTLP collector. |
| `SLEUTH_CLOUD_ENABLED` | `0` | Opt in to a cloud LLM fallback for Sleuth. |

All settings: [Key settings](docs/environment.md).

## 🛡️ Your data stays safe

- ✅ Upgrades only **add** tables and columns. Nothing is removed or rewritten.
- ✅ `./deploy.sh` takes a database dump first, and never touches the data volume.
- ✅ `./down.sh` keeps your data. Only `./down.sh --wipe-db` deletes it, after asking.
- ✅ Passwords use bcrypt. Uploads are checked by type and content.

Read [Live-data safety and deployment](docs/upgrading.md) and the [Security model](docs/security-model.md).

## 🧪 Quality

| Layer | Tooling |
|---|---|
| Backend | `pytest` on SQLite and PostgreSQL 16, with an 80% coverage gate |
| Frontend | Vitest, ESLint, axe-core accessibility checks in light and dark |
| Browser | Playwright suites for the main journeys and phone width |
| Security | ruff, bandit, pip-audit, gitleaks, Trivy, hadolint |
| Quality gate | SonarQube, run locally with `scripts/sonar-scan.*` |

```bash
pytest -m "not ui" -n auto --cov=app      # backend
cd frontend && npm test && npm run lint   # frontend
```

All commands and CI jobs: [Testing](docs/testing.md) · [Local development](docs/development.md).

## 📚 Documentation

| Topic | Page |
|---|---|
| Every feature and role | [features.md](docs/features.md) |
| Run it with Docker | [getting-started.md](docs/getting-started.md) |
| Sprints and boards | [agile.md](docs/agile.md) |
| Key settings, email, push, telemetry | [environment.md](docs/environment.md) |
| Deploy, backup, upgrade | [operations.md](docs/operations.md) · [upgrading.md](docs/upgrading.md) |
| Sleuth and Git integration | [sleuth.md](docs/sleuth.md) · [git-integration.md](docs/git-integration.md) |
| Security model | [security-model.md](docs/security-model.md) |
| Code layout | [architecture.md](docs/architecture.md) |
| Tests, CI and performance | [testing.md](docs/testing.md) · [development.md](docs/development.md) · [performance.md](docs/performance.md) |
| Fixes for common problems | [troubleshooting.md](docs/troubleshooting.md) |

Need several teams on one install, with billing? See **Bug Hunter Enterprise**.

## 🤝 Contributing, security, license

- Contributing: see [CONTRIBUTING.md](CONTRIBUTING.md). Run the tests before you open a pull request.
- Found a vulnerability? Report it privately, see [SECURITY.md](SECURITY.md).
- License: [MIT](LICENSE.txt).

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img alt="Bug Hunter" src="docs/assets/logo-light.svg" width="44">
  </picture>
</p>
