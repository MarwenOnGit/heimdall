# Hold-out scenarios

This directory is for 4 to 6 scenarios that are never used while developing
prompts, tools or thresholds. They give the honest generalization number in
the benchmark (spec sections 7.2 and 13.2).

To keep that number honest they should be written by someone who has not seen
the prompts, working only from the root cause categories and the scenario
format in `chaos/model.py`, and committed encrypted (`age` or `gpg`) until the
benchmark milestone.

None have been written yet.
