# Commands

## Setup (once)

```bash
cd /Users/anuprasjadhav/PycharmProjects/dev_agent                                                                                                                                                                                                                                                                          
source .venv/bin/activate                                                                                                                                                                                                                                                                                                  
createdb -h localhost ppl              # fresh real DB, separate from the test/scratch ones             
cd planning-pipeline
```

## Start

```bash
cd planning-pipeline
uvicorn src.api:app --reload --env-file .env
```

## Demo

```bash
python -m agent_tools.demo
```

## Tests / evals

```bash
pytest
python -m evals.run_evals
python -m evals.capture <run_id> --name <case_name>
```

## Multi-repo projects

### Interactive CLI (no server, no curl)

```bash
python -m src.cli
```
It asks for a project name and repos (git URLs or absolute paths, one per line),
clones and indexes them, then lets you type feature requests and prints which
repos each touches. Re-run with the same name to reuse a project. Non-interactive:

```bash
python -m src.cli shop --repo https://github.com/acme/api --repo /abs/path -q "add coupons"
```
`DATABASE_URL` and `LLM_API_KEY` are read from the environment or a `.env` in this folder.

### HTTP API

Name a project and give it repos as git URLs and/or absolute paths. URLs are
cloned under `REPOS_DIR` (default `~/.planning-pipeline/repos/<host>/<owner>/<repo>`)
using whatever git credentials this machine already has; all repos are then
indexed in the background.

```bash
export DATABASE_URL=postgresql+psycopg://<user>@localhost:5432/ppl
export LLM_API_KEY=sk-ant-...            # not ANTHROPIC_API_KEY

# create (blocks while cloning; indexing continues in the background)
curl -s -X POST localhost:8000/v1/projects -H 'Content-Type: application/json' -d '{
  "name": "shop",
  "repos": ["https://github.com/acme/orders-api", "git@github.com:acme/web.git", "/abs/path/to/other-repo"]
}'

# poll until every repo is "ready"
curl -s localhost:8000/v1/projects/1

# which repos does this request touch? (primary = change lives here, impacted = depends on it)
curl -s -X POST localhost:8000/v1/projects/1/select-repos -H 'Content-Type: application/json' \
     -d '{"text": "Add a discount_cents column to orders and show it at checkout"}'

# add another repo later (URL/path via "repo", or an already-registered one via "repo_id")
curl -s -X POST localhost:8000/v1/projects/1/repos -H 'Content-Type: application/json' \
     -d '{"repo": "https://github.com/acme/analytics-jobs"}'
```

Optional, set in `.env` (see `.env.example`) or the environment: `ROUTER_TIER` (`cheap` default, or `strong`), `ROUTER_MAX_TURNS` (default 12). The CLI reads `.env` automatically; for the server use `uvicorn src.api:app --env-file .env`.
Accepted URL forms: `https://…`, `ssh://…`, `git@host:owner/repo`, `file:///…`.
