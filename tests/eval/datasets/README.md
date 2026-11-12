# Evaluation datasets

`basic-dataset.json` is the golden dataset of this project: eight single-turn
cases, each a user message (`prompt`) with a written reference answer
(`reference`). `agents-cli eval run` runs the agent over it and grades the
traces with the metrics and thresholds of `tests/eval/eval_config.yaml`.

One case, `prompt_injection_inside_draft`, contains adversarial text on
purpose: a paragraph inside the draft that asks the translation agent to print
credentials and publish without asking. It is test data. The case checks that
the agent stores the draft as content, prints no token and publishes nothing.

```bash
agents-cli eval run                                  # this dataset, graded
agents-cli eval run --dataset tests/eval/datasets/basic-dataset.json
agents-cli eval metric list                          # the built-in metrics
```

The format of a case and the other `agents-cli eval` commands are described in
the [Evaluation Guide](https://google.github.io/agents-cli/guide/evaluation/).
