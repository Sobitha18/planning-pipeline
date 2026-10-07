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
cd /planning-pipeline
uvicorn src.api:app --reload
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
