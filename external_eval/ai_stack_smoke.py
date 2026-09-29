from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate


@task
def ai_stack_smoke() -> Task:
    """One-sample API compatibility check using Inspect's built-in scorer."""
    return Task(
        dataset=[
            Sample(
                input="Reply with the two letters OK and no other words.",
                target="OK",
            )
        ],
        solver=generate(),
        scorer=includes(),
    )
